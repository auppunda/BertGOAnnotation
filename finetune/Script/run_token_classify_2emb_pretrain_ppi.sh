
## using the newer code ... hopefully the fp16 works


## LM tune the data
# conda activate tensorflow_gpuenv
server='/local/datdb'
# data_dir=$server/'deepgo/data/DataToFinetuneBertTokenPredict/FinetunePhaseData'
mkdir $server/'deepgo/data/BertNotFtAARawSeqGO'

pretrained_label_path='/local/datdb/deepgo/data/cosine.AveWordClsSep768.Linear768.Layer12/label_vector.pickle'

choice='2embPpiGeluE768H1L12I768PretrainLabelDrop0.1' # Lr5e-5
block_size=1792 # mf and cc 1792 but bp has more term  2048
save_every=9500 # 9500

for ontology in 'mf' ; do
  last_save=$server/'deepgo/data/BertNotFtAARawSeqGO/'$ontology/'fold_1'/$choice
  output_dir=$server/'deepgo/data/BertNotFtAARawSeqGO/'$ontology/'fold_1'/$choice
  mkdir $output_dir

  # bert_vocab=$server/'BERTPretrainedModel/cased_L-12_H-768_A-12Kmer2016/vocab+3kmer+GO.txt'
  bert_vocab=$output_dir/'vocabAA.txt'
  # config_name=$server/'BERTPretrainedModel/cased_L-12_H-768_A-12Kmer2016/config.json'
  config_name=$output_dir/config.json
  model_name_or_path=$output_dir

  train_masklm_data='/local/datdb/deepgo/data/train/fold_1/TokenClassify/TwoEmb/train-'$ontology'-aa.csv' ## okay to call it as long as it has ppi
  eval_masklm_data='/local/datdb/deepgo/data/train/fold_1/TokenClassify/TwoEmb/dev-'$ontology'-aa.csv'
  label_2test='/local/datdb/deepgo/data/train/deepgo.'$ontology'.csv'

  cd $server/BertGOAnnotation/finetune/

  # 5040 batches train
  ## continue training use @model_name_or_path and turn off @config_override
  # CUDA_VISIBLE_DEVICES=7 python3 -u run_token_classify_2emb_ppi.py --block_size $block_size --mlm --bert_vocab $bert_vocab --train_data_file $train_masklm_data --output_dir $output_dir --num_train_epochs 100 --per_gpu_train_batch_size 6 --per_gpu_eval_batch_size 8 --config_name $config_name --do_train --model_type bert --overwrite_output_dir --save_steps $save_every --logging_steps $save_every --evaluate_during_training --eval_data_file $eval_masklm_data --label_2test $label_2test --config_override --learning_rate 1e-4 --seed 2019 --fp16 --pretrained_label_path $pretrained_label_path > $output_dir/train_point.txt # --no_cuda

  # # ## testing phase

  # for test_data in 'test'; do
  #   eval_masklm_data='/local/datdb/deepgo/data/train/fold_1/TokenClassify/TwoEmb/'$test_data'-'$ontology'-aa.csv'
  #   CUDA_VISIBLE_DEVICES=7 python3 -u run_token_classify_2emb_ppi.py --block_size $block_size --mlm --bert_vocab $bert_vocab --train_data_file $train_masklm_data --output_dir $output_dir --per_gpu_eval_batch_size 10 --config_name $config_name --do_eval --model_type bert --overwrite_output_dir --evaluate_during_training --eval_data_file $eval_masklm_data --label_2test $label_2test --config_override --eval_all_checkpoints --fp16 --pretrained_label_path $pretrained_label_path > $output_dir/'eval_'$test_data'_check_point.txt'
  # done 

  ## view weights ?? 

  cd $server/BertGOAnnotation/SeeAttention/
  eval_masklm_data='/local/datdb/deepgo/data/train/fold_1/TokenClassify/TwoEmb/train-'$ontology'-aa.csv'

  model_name_or_path=$output_dir'/checkpoint-40320'

  # CUDA_VISIBLE_DEVICES=6 python3 -u view_weight_2emb.py --block_size 1792 --mlm --bert_vocab $bert_vocab --train_data_file $train_masklm_data --output_dir $output_dir --per_gpu_eval_batch_size 6 --config_name $config_name --do_eval --model_type bert --overwrite_output_dir --evaluate_during_training --eval_data_file $eval_masklm_data --label_2test $label_2test --model_name_or_path $model_name_or_path --pretrained_label_path $pretrained_label_path > $output_dir/view_weights.txt

  CUDA_VISIBLE_DEVICES=7 python3 -u view_weight_aa_skewness.py --block_size $block_size --mlm --bert_vocab $bert_vocab --train_data_file $train_masklm_data --output_dir $output_dir --per_gpu_eval_batch_size 32 --config_name $config_name --do_eval --model_type bert --overwrite_output_dir --evaluate_during_training --eval_data_file $eval_masklm_data --label_2test $label_2test --model_name_or_path $model_name_or_path --pretrained_label_path $pretrained_label_path > $output_dir/view_aa_weights_train.txt


done


