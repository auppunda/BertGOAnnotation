

from __future__ import absolute_import, division, print_function

import argparse
import glob
import logging
import os
import pickle
import random
import re,sys
import pandas as pd

import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset, SequentialSampler, RandomSampler
from torch.utils.data.distributed import DistributedSampler
from tensorboardX import SummaryWriter
from tqdm import tqdm, trange

from pytorch_transformers import (WEIGHTS_NAME, AdamW, WarmupLinearSchedule,
                  BertConfig, BertForMaskedLM, BertTokenizer,
                  GPT2Config, GPT2LMHeadModel, GPT2Tokenizer,
                  OpenAIGPTConfig, OpenAIGPTLMHeadModel, OpenAIGPTTokenizer,
                  RobertaConfig, RobertaForMaskedLM, RobertaTokenizer)

from pytorch_transformers.modeling_bert import BertForPreTraining

sys.path.append("/local/datdb/BertGOAnnotation")
import KmerModel.TokenClassifier as TokenClassifier
import finetune.evaluation_metric as evaluation_metric

import view_util

logger = logging.getLogger(__name__)



def ReadProtData(string,num_aa,max_num_aa,annot_data,annot_name_sorted,evaluate):

  ## must do padding so all items get the same size
  out = np.zeros((max_num_aa,len(annot_name_sorted))) ## maximum possible
  if string == 'none':
    return coo_matrix(out)

  # @string is some protein data, delim by ";"
  annot = string.split(';')
  annot_matrix = np.zeros((num_aa,len(annot))) ## exact @num_aa without CLS and SEP

  for index, a in enumerate(annot): ## we do not need the whole matrix. we can use 1,2,3,4,5 indexing style on the column entry
    ## want annotation on protein sequence into matrix. Len x Type
    ## a = 'COILED 87-172;DOMAIN uba 2-42;DOMAIN ubx 211-293'.split(';')
    a = a.split() ## to get the position, which should always be at the last part
    type_name = " ".join( a[0 : (len(a)-1)] )

    if type_name not in annot_name_sorted: ## unseen Domain Type
      type_number = 1 ## set to UNK
    else:
      ## !! notice, shift by +2 so that we PAD=0 (nothing) and UNK=1 (some unseen domain)
      type_number = annot_name_sorted[ type_name ] ## make sure we exclude position which is last. @annot_name_sorted is index-lookup

    ## in preprocessing, we have -1, because uniprot give raw number, but python starts at 0.
    ## we do not -1 for the end point.
    row = [int(f) for f in a[-1].split('-')] ## get back 2 numbers
    row = np.arange(row[0]-1,row[1]) ## continuous segment

    ## we have to randomly assign UNK... assign a whole block of UNK
    ## do not need to assign random UNK for dev or test set
    if (not evaluate) and (type_number > 1): ## chance of being UNK
      if np.random.uniform() < annot_data[type_name][1]*1.0/annot_data[type_name][0]:
        annot_matrix [ row,index ] = 1 ## type=1 is UNK
      else:
        annot_matrix [ row,index ] = type_number ## @col is the number of the type w.r.t. the whole set of types, @index is just for this string
    else:
      annot_matrix [ row,index ] = type_number

  ## out is max_len (both aa + CSL SEP PAD) + len_annot
  ## read by row, so CLS has annot=0
  ## only need to shift 1 row down
  out[1:(num_aa+1), 0:len(annot)] = annot_matrix ## notice shifting by because of CLS and SEP

  # print ('\nsee annot matrix\n')
  # print (annot_matrix)
  return coo_matrix(out)


class TextDataset(Dataset):
  def __init__(self, tokenizer, label_2test_array, file_path='train', block_size=512, max_aa_len=1024, args=None, evaluate=None):
    # @max_aa_len is already cap at 1000 in deepgo, Facebook cap at 1024

    self.args = args

    assert os.path.isfile(file_path)
    directory, filename = os.path.split(file_path)
    if args.aa_type_emb:
      directory = os.path.join(directory , 'aa_ppi_annot_cache')
    else:
      directory = os.path.join(directory , 'aa_ppi_cache')
    if not os.path.exists(directory):
      os.mkdir(directory)

    cached_features_file = os.path.join(directory, f'cached_lm_{block_size}_{filename}')

    if os.path.exists(cached_features_file+'label1hot'): ## take 1 thing to test if it exists
      logger.info("Loading features from cached file %s", cached_features_file)
      with open(cached_features_file+'label1hot', 'rb') as handle:
        self.label1hot = pickle.load(handle)
      with open(cached_features_file+'input_ids_aa', 'rb') as handle:
        self.input_ids_aa = pickle.load(handle)
      with open(cached_features_file+'input_ids_label', 'rb') as handle:
        self.input_ids_label = pickle.load(handle)
      with open(cached_features_file+'mask_ids_aa', 'rb') as handle:
        self.mask_ids_aa = pickle.load(handle)
      with open(cached_features_file+'ppi_vec', 'rb') as handle:
        self.ppi_vec = pickle.load(handle)
      if args.aa_type_emb:
        with open(cached_features_file+'aa_type_emb', 'rb') as handle:
          self.aa_type_emb = pickle.load(handle)

    else:

      annot_name_sorted = None
      if args.aa_type_file is not None:
        print ('load in aa_type_file')
        annot_data = pickle.load ( open ( args.aa_type_file, 'rb' ) )
        temp = sorted (list (annot_data.keys() ) ) ## easiest to just do by alphabet, so we can backtrack very easily
        ## make sure we exclude position which is last. @annot_name_sorted is index-lookup, so we have +2
        annot_name_sorted = {value: index+2 for index, value in enumerate(temp)} ## lookup index


      label_2test_array = sorted(label_2test_array) ## just to be sure we keep alphabet
      num_label = len(label_2test_array)
      print ('num label {}'.format(num_label))
      label_index_map = { name : index for index,name in enumerate(label_2test_array) } ## faster look up
      # label_string = " ".join(label_2test_array)

      logger.info("Creating features from dataset file at %s", directory)

      self.label1hot = [] ## take 1 hot (should short labels by alphabet)
      self.input_ids_aa = []
      self.input_ids_label = []
      self.mask_ids_aa = []
      self.ppi_vec = [] ## some vector on the prot-prot interaction network... or something like that
      if args.aa_type_emb:
        self.aa_type_emb = []

      fin = open(file_path,"r",encoding='utf-8')
      for counter, text in tqdm(enumerate(fin)):

        # if counter > 100 :
        #   break

        text = text.strip()
        if len(text) == 0: ## skip blank ??
          continue

        ## we test all the labels in 1 single call, so we have to always get all the labels.
        ## notice we shift 1+ so that we can have padding at 0.
        # self.input_ids_label.append ( list((1+np.arange(num_label+1))) )  ## add a SEP to end of label side ??? Okay, add SEP
        self.input_ids_label.append ( np.arange(num_label).tolist() )  ## add a SEP to end of label side ??? Okay, add SEP

        ## split at \t ?? [seq \t label]
        text = text.split("\t") ## position 0 is kmer sequence, position 1 is list of labels

        ### !!!!
        ### !!!! now we append the protein-network vector
        self.ppi_vec.append ([float(s) for s in text[3].split()]) ## 3rd tab

        ## create a gold-standard label 1-hot vector.
        ## convert label into 1-hot style
        label1hot = np.zeros(num_label) ## 1D array
        this_label = text[2].strip().split() ## by space
        index_as1 = [label_index_map[label] for label in this_label]
        label1hot [ index_as1 ] = 1
        self.label1hot.append( label1hot )

        # kmer_text = text[0].split() ## !! we must not use string text, otherwise, we will get wrong len
        ## GET THE AA INDEXING '[CLS] ' + text[0] + ' [SEP]'
        this_aa = tokenizer.convert_tokens_to_ids ( tokenizer.tokenize ('[CLS] ' + text[1] + ' [SEP]') )
        len_withClsSep = len(this_aa)

        ## pad @this_aa to max len
        mask_aa = [1] * len_withClsSep + [0] * ( max_aa_len - len_withClsSep ) ## attend to non-pad
        this_aa = this_aa + [0] * ( max_aa_len - len_withClsSep ) ## padding
        self.input_ids_aa.append( this_aa )
        self.mask_ids_aa.append (mask_aa)

        if args.aa_type_emb:
          ### !!! need to get token type emb of AA in protein
          ## in evaluation mode, do not need to random assign UNK
          AA = ReadProtData(text[4],len_withClsSep-2,max_aa_len,annot_data,annot_name_sorted,evaluate=evaluate)
          self.aa_type_emb.append ( AA )

        if counter < 3:
          print ('see sample {}'.format(counter))
          print (this_aa)
          print (label1hot)
          print (self.ppi_vec[counter])

        if (len(this_aa) + num_label) > block_size:
          print ('len too long, expand block_size')
          exit()

      ## save at end
      logger.info("To save read/write time... Saving features into cached file %s", cached_features_file)
      with open(cached_features_file+'label1hot', 'wb') as handle:
        pickle.dump(self.label1hot, handle, protocol=pickle.HIGHEST_PROTOCOL)
      with open(cached_features_file+'input_ids_aa', 'wb') as handle:
        pickle.dump(self.input_ids_aa, handle, protocol=pickle.HIGHEST_PROTOCOL)
      with open(cached_features_file+'input_ids_label', 'wb') as handle:
        pickle.dump(self.input_ids_label, handle, protocol=pickle.HIGHEST_PROTOCOL)
      with open(cached_features_file+'mask_ids_aa', 'wb') as handle:
        pickle.dump(self.mask_ids_aa, handle, protocol=pickle.HIGHEST_PROTOCOL)
      with open(cached_features_file+'ppi_vec', 'wb') as handle:
          pickle.dump(self.ppi_vec, handle, protocol=pickle.HIGHEST_PROTOCOL)

      if args.aa_type_emb:
        with open(cached_features_file+'aa_type_emb', 'wb') as handle:
          pickle.dump(self.aa_type_emb, handle, protocol=pickle.HIGHEST_PROTOCOL)

  def __len__(self):
    return len(self.input_ids_aa)

  def __getitem__(self, item):
    if self.args.aa_type_emb:
      return (torch.LongTensor(self.label1hot[item]),
              torch.tensor(self.input_ids_aa[item]),
              torch.tensor(self.input_ids_label[item]),
              torch.tensor(self.mask_ids_aa[item]),
              torch.tensor(self.ppi_vec[item]),
              torch.LongTensor(self.aa_type_emb[item].toarray()))
    else:
      return (torch.LongTensor(self.label1hot[item]),
              torch.tensor(self.input_ids_aa[item]),
              torch.tensor(self.input_ids_label[item]),
              torch.tensor(self.mask_ids_aa[item]),
              torch.tensor(self.ppi_vec[item]) )


def load_and_cache_examples(args, tokenizer, label_2test_array, evaluate=False):
  dataset = TextDataset(tokenizer, label_2test_array, file_path=args.eval_data_file if evaluate else args.train_data_file, block_size=args.block_size, args=args, evaluate=evaluate)
  return dataset


def main():
  parser = argparse.ArgumentParser()

  parser.add_argument("--data_type", type=str, default=None)
  parser.add_argument("--aa_type_file", type=str, default=None)
  parser.add_argument("--pretrained_label_path", type=str, default=None)
  parser.add_argument("--label_2test", type=str, default=None)
  parser.add_argument("--bert_vocab", type=str, default=None)
  parser.add_argument("--config_override", action="store_true")
  parser.add_argument("--aa_type_emb", action="store_true", default=False)

  ## Required parameters
  parser.add_argument("--train_data_file", default=None, type=str,
            help="The input training data file (a text file).")
  parser.add_argument("--output_dir", default=None, type=str,
            help="The output directory where the model predictions and checkpoints will be written.")

  ## Other parameters
  parser.add_argument("--eval_data_file", default=None, type=str,
            help="An optional input evaluation data file to evaluate the perplexity on (a text file).")

  parser.add_argument("--model_type", default="bert", type=str,
            help="The model architecture to be fine-tuned.")
  parser.add_argument("--model_name_or_path", default="bert-base-cased", type=str,
            help="The model checkpoint for weights initialization.")

  parser.add_argument("--mlm", action='store_true',
            help="Train with masked-language modeling loss instead of language modeling.")
  parser.add_argument("--mlm_probability", type=float, default=0.15,
            help="Ratio of tokens to mask for masked language modeling loss")

  parser.add_argument("--config_name", default="", type=str,
            help="Optional pretrained config name or path if not the same as model_name_or_path")
  parser.add_argument("--tokenizer_name", default="", type=str,
            help="Optional pretrained tokenizer name or path if not the same as model_name_or_path")
  parser.add_argument("--cache_dir", default="", type=str,
            help="Optional directory to store the pre-trained models downloaded from s3 (instread of the default one)")
  parser.add_argument("--block_size", default=-1, type=int,
            help="Optional input sequence length after tokenization."
               "The training dataset will be truncated in block of this size for training."
               "Default to the model max input length for single sentence inputs (take into account special tokens).")
  parser.add_argument("--do_train", action='store_true',
            help="Whether to run training.")
  parser.add_argument("--do_eval", action='store_true',
            help="Whether to run eval on the dev set.")
  parser.add_argument("--evaluate_during_training", action='store_true',
            help="Run evaluation during training at each logging step.")
  parser.add_argument("--do_lower_case", action='store_true',
            help="Set this flag if you are using an uncased model.")

  parser.add_argument("--per_gpu_train_batch_size", default=4, type=int,
            help="Batch size per GPU/CPU for training.")
  parser.add_argument("--per_gpu_eval_batch_size", default=4, type=int,
            help="Batch size per GPU/CPU for evaluation.")
  parser.add_argument('--gradient_accumulation_steps', type=int, default=1,
            help="Number of updates steps to accumulate before performing a backward/update pass.")
  parser.add_argument("--learning_rate", default=5e-5, type=float,
            help="The initial learning rate for Adam.")
  parser.add_argument("--weight_decay", default=0.0, type=float,
            help="Weight deay if we apply some.")
  parser.add_argument("--adam_epsilon", default=1e-8, type=float,
            help="Epsilon for Adam optimizer.")
  parser.add_argument("--max_grad_norm", default=1.0, type=float,
            help="Max gradient norm.")
  parser.add_argument("--num_train_epochs", default=1.0, type=float,
            help="Total number of training epochs to perform.")
  parser.add_argument("--max_steps", default=-1, type=int,
            help="If > 0: set total number of training steps to perform. Override num_train_epochs.")
  parser.add_argument("--warmup_steps", default=0, type=int,
            help="Linear warmup over warmup_steps.")

  parser.add_argument('--logging_steps', type=int, default=50,
            help="Log every X updates steps.")
  parser.add_argument('--save_steps', type=int, default=50,
            help="Save checkpoint every X updates steps.")
  parser.add_argument("--eval_all_checkpoints", action='store_true',
            help="Evaluate all checkpoints starting with the same prefix as model_name_or_path ending and ending with step number")
  parser.add_argument("--no_cuda", action='store_true',
            help="Avoid using CUDA when available")
  parser.add_argument('--overwrite_output_dir', action='store_true',
            help="Overwrite the content of the output directory")
  parser.add_argument('--overwrite_cache', action='store_true',
            help="Overwrite the cached training and evaluation sets")
  parser.add_argument('--seed', type=int, default=42,
            help="random seed for initialization")

  parser.add_argument('--fp16', action='store_true',
            help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit")
  parser.add_argument('--fp16_opt_level', type=str, default='O1',
            help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
               "See details at https://nvidia.github.io/apex/amp.html")
  parser.add_argument("--local_rank", type=int, default=-1,
            help="For distributed training: local_rank")
  parser.add_argument('--server_ip', type=str, default='', help="For distant debugging.")
  parser.add_argument('--server_port', type=str, default='', help="For distant debugging.")
  args = parser.parse_args()


  ## does weight make sense ?

  # read in labels to be testing
  label_2test_array = pd.read_csv(args.label_2test,header=None)
  label_2test_array = sorted(list( label_2test_array[0] ))
  label_2test_array = [re.sub(":","",lab) for lab in label_2test_array] ## splitting has problem with the ":"
  num_labels = len(label_2test_array)


  config = BertConfig.from_pretrained(args.model_name_or_path)
  config.output_attentions=True ## override @config
  # config.output_hidden_states=True

  tokenizer = BertTokenizer.from_pretrained(args.bert_vocab, do_lower_case=args.do_lower_case)

  model = TokenClassifier.BertForTokenClassification2EmbPPI.from_pretrained(args.model_name_or_path, config=config) ## use @config=config to override the default @config

  model.cuda() ## ?? do we need to send to gpu

  ## create dataset again.
  ## must respect the ordering of GO terms.

  eval_dataset = load_and_cache_examples(args, tokenizer, label_2test_array, evaluate=True)
  args.eval_batch_size = args.per_gpu_eval_batch_size ## just use 1 gpu
  eval_sampler = SequentialSampler(eval_dataset)
  eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler, batch_size=args.eval_batch_size)

  number_sequences = len(eval_dataset)
  print ("\nnumber seqs {}\n".format(number_sequences))

  ## create @label_names_in_index
  ## to test GO-vs-AA, we need to narrow down the set of GO a little bit otherwise, we have too much data that we can't "aggregate"
  ## redefine @label_2test_array
  # label_2test_array = sorted ( ['GO0002039','GO0000287','GO0000049'] ) ## add more later
  label_names_in_index = view_util.get_word_index_in_array(tokenizer,label_2test_array) ## these are the word_index we will need to extract

  letters = 'A, E, I, O, U, B, C, D, F, G, H, J, K, L, M, N, P, Q, R, S, T, V, X, Z, W, Y'.split(',')
  letters = sorted ( [let.strip() for let in letters] )
  AA_names_in_index = view_util.get_word_index_in_array(tokenizer,letters) ## these are word_index we want. we don't need to extract CLS and SEP ... but they can probably be important ??


  if args.aa_type_emb: # model mutation
    # protein_name = pd.read_csv("/local/datdb/deepgo/data/train/fold_1/train-mf-mut.tsv", dtype=str, sep="\t",index_col=0)
    # protein_name = list ( protein_name['Entry'] )
    # prot_change = 'P56817 Q0WP12 O43824 Q6ZPK0'.split()
    # protein_name = [p for p in protein_name if p not in prot_change]

    protein_name = pd.read_csv("/local/datdb/deepgo/data/train/fold_1/TokenClassify/TwoEmb/"+args.data_type+"-mf-prot-annot.tsv", dtype=str, sep="\t",header=None)
    protein_name = list ( protein_name[0] )

  else:
    protein_name = pd.read_csv("/local/datdb/deepgo/data/train/fold_1/"+args.data_type+"-mf.tsv", dtype=str, sep="\t")
    protein_name = list ( protein_name['Entry'] )


  ## what do we need to keep ??

  eval_loss = 0.0
  nb_eval_steps = 0
  model.eval()

  if not os.path.exists( os.path.join(args.output_dir,"ManualValidate") )  :
    os.mkdir ( os.path.join(args.output_dir,"ManualValidate") )

  # get attention head for sequence
  # GO2GO_attention = {}
  # GO2AA_attention = {} ##  { name: {head:[range]} }
  # GO2AA_attention_quantile = {}
  # GO2all_attention = {}

  row_counter = 0 # so we can check the row id.

  # list_prot_to_get = ['O54992','P23109','P9WNC3']
  # list_prot_to_get = np.random.choice(protein_name, size=100, replace=False, p=None).tolist()
  # list_prot_to_get = list( set ( sorted (list_prot_to_get) + ['O54992'] ) ) ## seem kinda stupid
  
  list_prot_to_get = """O35730
  O35730
  O35730
  O54992
  O54992
  O54992
  P0A812
  P0A812
  P0A812
  Q5VV41
  Q5VV41
  Q5VV41
  Q6FJA3
  Q6FJA3
  Q6FJA3
  Q6X632
  Q6X632
  Q6X632
  Q96B01
  Q96B01
  Q96B01
  Q9HWK6
  Q9HWK6
  Q9HWK6
  Q9S9K9
  Q9S9K9
  Q9S9K9"""
  list_prot_to_get = list ( set ( re.sub(r"\n"," ",list_prot_to_get).split() ) ) 



  for batch_counter,batch in tqdm(enumerate(eval_dataloader), desc="Evaluating"):

    batch_size = batch[1].shape[0] ## do only what needed

    if (batch_counter*batch_size) != row_counter:
      print ('check @row_counter for @batch_counter {} should see this message only at the last batch'.format(batch_counter))

    end = row_counter + batch_size
    if len( set(protein_name[row_counter:end]).intersection( set(list_prot_to_get) ) ) == 0 :
      ## suppose we don't enter this @if, then we will update @row_counter at the end, before we call "@for batch_counter...."
      row_counter = end ## skip all, start at new positions of next batch
      continue

    max_len_in_batch = int( torch.max ( torch.sum(batch[3],1) ) ) ## only need max len of AA
    input_ids_aa = batch[1][:,0:max_len_in_batch].cuda()
    input_ids_label = batch[2].cuda()
    attention_mask = torch.cat( (batch[3][:,0:max_len_in_batch] , torch.ones(input_ids_label.shape,dtype=torch.long) ), dim=1 ).cuda()

    labels = batch[0].cuda() ## already in batch_size x num_label
    ## must append 0 positions to the front, so that we mask out AA
    labels_mask = torch.cat((torch.zeros(input_ids_aa.shape),
      torch.ones(input_ids_label.shape)),dim=1).cuda() ## test all labels

    ppi_vec = batch[4].unsqueeze(1).expand(labels.shape[0],max_len_in_batch+num_labels,256).cuda() ## make

    if args.aa_type_emb:
      aa_type = batch[5][:,0:max_len_in_batch,:].cuda()
    else:
      aa_type = None

    with torch.no_grad():
      outputs = model(0, input_ids_aa=input_ids_aa, input_ids_label=input_ids_label, token_type_ids=aa_type, attention_mask=attention_mask, labels=labels, position_ids=None, attention_mask_label=labels_mask, prot_vec=ppi_vec )
      lm_loss = outputs[0]
      eval_loss += lm_loss.mean().item()

    nb_eval_steps += 1

    attention_mask = attention_mask.detach().cpu().numpy() ## used to get back only important positions

    layer_att = outputs[-1] ## @outputs is a tuple of loss, prediction score, attention ... we use [-1] to get @attention.
    print ('len @layer_att {}'.format(len(layer_att))) ## each layer is one entry in this tuple

    GO2all_attention = {} ## create one dictionary for each prot. (large data size, but we can trim/delete later)

    for layer in range (config.num_hidden_layers):

      this_layer_att = layer_att[layer].detach().cpu().numpy() ## @layer_att is a tuple

      # num_batch x num_head x word x word
      # we get each obs in the batch, and get the #head
      for obs in range(batch_size): # will be #obs x #head x #word x #word

        this_prot_name = protein_name[row_counter+obs]

        if this_prot_name in list_prot_to_get:

          if layer == 0: ## sanity check
            print ("\n")
            print (this_prot_name)
            print (max_len_in_batch)

          where_not_mask = attention_mask[obs]==1

          if this_prot_name not in GO2all_attention:
            GO2all_attention[this_prot_name] = {}

          # do not need to do this again
          GO2all_attention[ this_prot_name ][layer] = {}

          for head in range(config.num_attention_heads) : # range(config.num_attention_heads):
            save = this_layer_att[obs][head]
            ## must use masking to get back correct values
            GO2all_attention[ this_prot_name ][layer][head] = save [ :, where_not_mask ] [ where_not_mask, : ]
            print (GO2all_attention[ this_prot_name ][layer][head].shape[0] - 2 - num_labels) # quality check


    for k in GO2all_attention:
      out = {}
      out[k] = GO2all_attention[k]
      if len(out[k])!=12:
        print ('fail {}'.format(k))
      else:
        pickle.dump(out, open(os.path.join(args.output_dir,"ManualValidate/"+args.data_type+"_attention_"+k+".pickle"), 'wb') )

    ## update next counter, so we move to batch#2 in the raw text
    row_counter = end




if __name__ == "__main__":
  main()

