import argparse
import gc

gc.enable()
from functools import partial

import pandas as pd
import torch
from datasets import Dataset
from torch import nn
from torch.nn import CrossEntropyLoss
from transformers import AutoTokenizer, RemBertForQuestionAnswering, TrainingArguments, default_data_collator
from transformers.modeling_outputs import QuestionAnsweringModelOutput

from .utils import CustomTrainer, SaveBestModelCallback, convert_answers, jaccardScore, prepare_train_features

MODEL_CHECKPOINT = "google/rembert"
POST_FIX = "_REMBERT_WITH_SQUADV2_TYDIQA_384"

EVALUATION_STRATEGY = "steps"
LEARNING_RATE = 1e-5
WARMUP_RATIO = 0.2
GRADIENT_ACCUMULATION_STEPS = 1
BATCH_SIZE = 8
EPOCH = 1
LOGGING_STEPS = 500
WEIGHT_DECAY = 0.01
MAX_LENGTH = 384  # The maximum length of a feature (question and context)
DOC_STRIDE = 128  # The authorized overlap between two part of the context when splitting it is needed


tokenizer = AutoTokenizer.from_pretrained(MODEL_CHECKPOINT)
pad_on_right = tokenizer.padding_side == "right"


class CustomRemBertForQuestionAnswering(RemBertForQuestionAnswering):
    def __init__(self, config):
        super().__init__(config)
        self.end_outputs = nn.Linear(config.hidden_size, 1)
        self.start_outputs = nn.Linear(config.hidden_size + 1, 1)

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        start_positions=None,
        end_positions=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.rembert(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        sequence_output = outputs[0]
        #################### Modified stuff ############################
        end_logits = self.end_outputs(sequence_output)
        start_logits_ = torch.cat((sequence_output, end_logits), dim=-1)
        start_logits = self.start_outputs(start_logits_)
        ################################################################

        start_logits = start_logits.squeeze(-1).contiguous()
        end_logits = end_logits.squeeze(-1).contiguous()

        total_loss = None
        if start_positions is not None and end_positions is not None:
            # If we are on multi-GPU, split add a dimension
            if len(start_positions.size()) > 1:
                start_positions = start_positions.squeeze(-1)
            if len(end_positions.size()) > 1:
                end_positions = end_positions.squeeze(-1)
            # sometimes the start/end positions are outside our model inputs, we ignore these terms
            ignored_index = start_logits.size(1)
            start_positions = start_positions.clamp(0, ignored_index)
            end_positions = end_positions.clamp(0, ignored_index)

            loss_fct = CrossEntropyLoss(ignore_index=ignored_index)
            start_loss = loss_fct(start_logits, start_positions)
            end_loss = loss_fct(end_logits, end_positions)
            total_loss = (start_loss + end_loss) / 2

        if not return_dict:
            output = (start_logits, end_logits) + outputs[2:]
            return ((total_loss,) + output) if total_loss is not None else output

        return QuestionAnsweringModelOutput(
            loss=total_loss,
            start_logits=start_logits,
            end_logits=end_logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


def train_fold(fold, model_class):
    print(f"==> Starting FOLD {fold}")

    train = pd.read_csv("../input/train_folds.csv")
    valid = train[train.kfold == fold].reset_index(drop=True)
    train = train[train.kfold != fold].reset_index(drop=True)

    external_mlqa = pd.read_csv("../input/mlqa_hindi.csv")
    external_xquad = pd.read_csv("../input/xquad.csv")
    external_train = pd.concat([external_mlqa, external_xquad])
    external_train["fold"] = -1
    external_train = external_train.reset_index().rename(columns={"index": "id"})
    external_train["id"] = "id" + external_train["id"].astype(str)

    squadv2 = pd.read_csv("../input/squadv2.csv").dropna()
    tydiqa = pd.read_csv("../input/tydiqa.csv").dropna()

    train = pd.concat([tydiqa, squadv2, external_train] + 5 * [train]).reset_index(drop=True)

    train["answers"] = train[["answer_start", "answer_text"]].apply(convert_answers, axis=1)
    valid["answers"] = valid[["answer_start", "answer_text"]].apply(convert_answers, axis=1)

    train_dataset = Dataset.from_pandas(train)
    valid_dataset = Dataset.from_pandas(valid)

    prep_train_features = partial(
        prepare_train_features,
        tokenizer=tokenizer,
        pad_on_right=pad_on_right,
        max_len=MAX_LENGTH,
        doc_stride=DOC_STRIDE,
    )

    tokenized_train_ds = train_dataset.map(
        prep_train_features, batched=True, remove_columns=train_dataset.column_names
    )
    tokenized_valid_ds = valid_dataset.map(
        prep_train_features, batched=True, remove_columns=valid_dataset.column_names
    )

    model = model_class.from_pretrained(MODEL_CHECKPOINT)

    args = TrainingArguments(
        f"models{POST_FIX}/FOLD{fold}",
        evaluation_strategy=EVALUATION_STRATEGY,
        save_strategy="no",
        learning_rate=LEARNING_RATE,
        warmup_ratio=WARMUP_RATIO,
        gradient_accumulation_steps=GRADIENT_ACCUMULATION_STEPS,
        label_smoothing_factor=0,
        per_device_train_batch_size=BATCH_SIZE,
        per_device_eval_batch_size=128,
        num_train_epochs=EPOCH,
        weight_decay=WEIGHT_DECAY,
        logging_steps=LOGGING_STEPS,
        dataloader_num_workers=10,
        report_to="tensorboard",
        fp16=True,
        save_total_limit=1,
        label_names=["start_positions", "end_positions"],
    )

    trainer = CustomTrainer(
        model,
        args,
        train_dataset=tokenized_train_ds,
        eval_dataset=tokenized_valid_ds,
        data_collator=default_data_collator,
        tokenizer=tokenizer,
        compute_metrics=jaccardScore(
            valid,
            tokenizer,
            pad_on_right=pad_on_right,
            max_len=MAX_LENGTH,
            doc_stride=DOC_STRIDE,
        ),
        callbacks=[SaveBestModelCallback],
    )

    trainer.train()

    del model, trainer, tokenized_train_ds, tokenized_valid_ds, train, valid, train_dataset, valid_dataset
    gc.collect()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fold", type=int)
    args = ap.parse_args()
    MODEL_CLASS = CustomRemBertForQuestionAnswering
    train_fold(args.fold, model_class=MODEL_CLASS)
