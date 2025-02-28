declare -a seed=(5)
declare -a device=(0)

for ((i=0;i<${#seed[@]};++i)); do
WANDB_MODE=online CUDA_VISIBLE_DEVICES=${device[0]} python ../../main2.py --root=../../../../da_datasets/pacs --batch-size=48 \
-d PACS -s C,S,A -t P -a resnet18 \
--name=PACS_test_To_P \
--z_dim=64 \
-i=1000 \
--seed=${seed[i]} \
--train_batch_size=16 \
--train_epochs=10 \
--finetune_epochs=5 \
--decouple_alpha=1.0 --decouple_beta=10.0 \
--phase=train

done



