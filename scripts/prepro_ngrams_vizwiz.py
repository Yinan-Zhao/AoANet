"""
Preprocess a raw json dataset into hdf5/json files for use in data_loader.lua

Input: json file that has the form
[{ file_path: 'path/img.jpg', captions: ['a caption', ...] }, ...]
example element in this list would look like
{'captions': [u'A man with a red helmet on a small moped on a dirt road. ', u'Man riding a motor bike on a dirt road on the countryside.', u'A man riding on the back of a motorcycle.', u'A dirt path with a young person on a motor bike rests to the foreground of a verdant area with a bridge and a background of cloud-wreathed mountains. ', u'A man in a red shirt and a red hat is on a motorcycle on a hill side.'], 'file_path': u'val2014/COCO_val2014_000000391895.jpg', 'id': 391895}

This script reads this json, does some basic preprocessing on the captions
(e.g. lowercase, etc.), creates a special UNK token, and encodes everything to arrays

Output: a json file and an hdf5 file
The hdf5 file contains several fields:
/images is (N,3,256,256) uint8 array of raw image data in RGB format
/labels is (M,max_length) uint32 array of encoded labels, zero padded
/label_start_ix and /label_end_ix are (N,) uint32 arrays of pointers to the 
  first and last indices (in range 1..M) of labels for each image
/label_length stores the length of the sequence for each of the M sequences

The json file has a dict that contains:
- an 'ix_to_word' field storing the vocab in form {ix:'word'}, where ix is 1-indexed
- an 'images' field that is a list holding auxiliary information for each image, 
  such as in particular the 'split' it was assigned to.
"""

import os
import re
import sys
import json
import argparse
import string
import six
from six.moves import cPickle
from collections import defaultdict

sys.path.append('/home/yz9244/Up-Down-Captioner/external/coco/PythonAPI/')
from pycocotools.coco import COCO

VizWiz_ANN_PATH = '/home/yz9244/Up-Down-Captioner/bottom-up-attention/data/VizWiz/annotations/'

#corrupt_list = [37093]
corrupt_list = []

SENTENCE_SPLIT_REGEX = re.compile(r'(\W+)') # Split on any non-alphanumeric character

def pickle_dump(obj, f):
    """ Dump a pickle.
    Parameters
    ----------
    obj: pickled object
    f: file-like object
    """
    if six.PY3:
        return cPickle.dump(obj, f, protocol=2)
    else:
        return cPickle.dump(obj, f)

def split_sentence(sentence):
  """ break sentence into a list of words and punctuation """
  toks = []
  for word in [s.strip().lower() for s in SENTENCE_SPLIT_REGEX.split(sentence.strip()) if len(s.strip()) > 0]:
    # Break up any words containing punctuation only, e.g. '!?', unless it is multiple full stops e.g. '..'
    if all(c in string.punctuation for c in word) and not all(c in '.' for c in word):
      toks += list(word)
    else:
      toks.append(word)
  # Remove '.' from the end of the sentence - 
  # this is EOS token that will be populated by data layer
  if toks[-1] != '.':
    return toks
  return toks[:-1]

def precook(s, n=4, out=False):
  """
  Takes a string as input and returns an object that can be given to
  either cook_refs or cook_test. This is optional: cook_refs and cook_test
  can take string arguments as well.
  :param s: string : sentence to be converted into ngrams
  :param n: int    : number of ngrams for which representation is calculated
  :return: term frequency vector for occuring ngrams
  """
  words = s.split()
  counts = defaultdict(int)
  for k in range(1,n+1):
    for i in range(len(words)-k+1):
      ngram = tuple(words[i:i+k])
      counts[ngram] += 1
  return counts

def cook_refs(refs, n=4): ## lhuang: oracle will call with "average"
    '''Takes a list of reference sentences for a single segment
    and returns an object that encapsulates everything that BLEU
    needs to know about them.
    :param refs: list of string : reference sentences for some image
    :param n: int : number of ngrams for which (ngram) representation is calculated
    :return: result (list of dict)
    '''
    return [precook(ref, n) for ref in refs]

def create_crefs(refs):
  crefs = []
  for ref in refs:
    # ref is a list of 5 captions
    crefs.append(cook_refs(ref))
  return crefs

def compute_doc_freq(crefs):
  '''
  Compute term frequency for reference data.
  This will be used to compute idf (inverse document frequency later)
  The term frequency is stored in the object
  :return: None
  '''
  document_frequency = defaultdict(float)
  for refs in crefs:
    # refs, k ref captions of one image
    for ngram in set([ngram for ref in refs for (ngram,count) in ref.items()]):
      document_frequency[ngram] += 1
      # maxcounts[ngram] = max(maxcounts.get(ngram,0), count)
  return document_frequency

def build_dict(wtoi, params):
  wtoi['<eos>'] = 0

  count_imgs = 0

  refs_words = []
  refs_idxs = []

  for dataset in params['split']:
    annFile='%s/VizWiz_Captions_v1_%s.json' % (VizWiz_ANN_PATH, dataset)
    coco = COCO(annFile)
    for image_id,anns in coco.imgToAnns.iteritems():
      if image_id in corrupt_list:
        continue
      ref_words = []
      ref_idxs = []
      for j, ann in enumerate(anns):
        caption_sequence = split_sentence(ann['caption'])
        if hasattr(params, 'bpe'):
          caption_sequence = params.bpe.segment(' '.join(caption_sequence)).strip().split(' ')
        tmp_tokens = caption_sequence + ['<eos>']
        tmp_tokens = [_ if _ in wtoi else 'UNK' for _ in tmp_tokens]
        ref_words.append(' '.join(tmp_tokens))
        ref_idxs.append(' '.join([str(wtoi[_]) for _ in tmp_tokens]))
      refs_words.append(ref_words)
      refs_idxs.append(ref_idxs)
      count_imgs += 1
  print('total imgs:', count_imgs)

  ngram_words = compute_doc_freq(create_crefs(refs_words))
  ngram_idxs = compute_doc_freq(create_crefs(refs_idxs))
  return ngram_words, ngram_idxs, count_imgs

def main(params):

  dict_json = json.load(open(params['dict_json'], 'r'))
  itow = dict_json['ix_to_word']
  wtoi = {w:i for i,w in itow.items()}

  # Load bpe
  if 'bpe' in dict_json:
    import tempfile
    import codecs
    codes_f = tempfile.NamedTemporaryFile(delete=False)
    codes_f.close()
    with open(codes_f.name, 'w') as f:
      f.write(dict_json['bpe'])
    with codecs.open(codes_f.name, encoding='UTF-8') as codes:
      bpe = apply_bpe.BPE(codes)
    params.bpe = bpe

  ngram_words, ngram_idxs, ref_len = build_dict(wtoi, params)

  pickle_dump({'document_frequency': ngram_words, 'ref_len': ref_len}, open(params['output_pkl']+'-words.p','wb'))
  pickle_dump({'document_frequency': ngram_idxs, 'ref_len': ref_len}, open(params['output_pkl']+'-idxs.p','wb'))

if __name__ == "__main__":

  parser = argparse.ArgumentParser()

  parser.add_argument('--dict_json', default='data/vizwiztalk.json', help='output json file')
  parser.add_argument('--output_pkl', default='data/vizwiz-train', help='output pickle file')
  parser.add_argument('--split', default='all', help='test, val, train, all')
  args = parser.parse_args()
  params = vars(args) # convert to ordinary dict

  sys.path.append("/home/yz9244/AoANet/")
  params['split'] = ['train', 'val']

  main(params)
