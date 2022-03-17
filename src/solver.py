import torch
import numpy as np
import torch.nn as nn
import torch.optim as optim
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
from itertools import compress
import time
import sys

from models.ipa2lt_head import Ipa2ltHead
from models.basic import BasicNetwork
from utils import get_model_path


class Solver(object):

    def __init__(self, dataset, learning_rate, batch_size, momentum=0.9, model_weights_path='',
                 writer=None, device=torch.device('cpu'), loss='cross', verbose=True,
                 embedding_dim=50, label_dim=2, annotator_dim=2, averaging_method='macro',
                 save_path_head=None, save_at=None, save_params=None, use_softmax=True,
                 pseudo_annotators=None, pseudo_model_path_func=None, pseudo_func_args={},
                 optimizer_name='adam', early_stopping_margin=1e-4,
                 ):
        self.learning_rate = learning_rate
        self.batch_size = batch_size
        self.dataset = dataset
        self.embedding_dim = embedding_dim
        self.label_dim = label_dim
        self.annotator_dim = annotator_dim
        self.momentum = momentum
        self.model_weights_path = model_weights_path
        self.save_path_head = save_path_head
        self.save_at = save_at
        self.save_params = save_params
        self.device = device
        self.writer = writer
        self.verbose = verbose
        self.averaging_method = averaging_method
        self.use_softmax = use_softmax
        self.early_stopping_margin = early_stopping_margin

        # can either be 'cross', 'bce', 'nll' or 'nll_log' (different versions of cross entropy loss)
        self.loss = loss

        # can either be 'sgd' or 'adam'
        self.optimizer_name = optimizer_name

        # List with pseudo annotators and separate function for getting a model path
        self.pseudo_annotators = pseudo_annotators
        self.pseudo_model_path_func = pseudo_model_path_func
        self.pseudo_func_args = pseudo_func_args

        if pseudo_annotators is not None:
            self._create_pseudo_labels()

        if self.device.type == 'cpu':
            from datasets import collate_wrapper_cpu as collate_wrapper
        elif self.device.type == 'cuda':
            from datasets import collate_wrapper
        self.collate_wrapper = collate_wrapper

    def _get_model(self, basic_only=False, pretrained_basic=False):
        if not basic_only:
            model = Ipa2ltHead(self.embedding_dim, self.label_dim,
                               self.annotator_dim, use_softmax=self.use_softmax, apply_log=self.loss == 'nll_log')
        else:
            model = BasicNetwork(self.embedding_dim,
                                 self.label_dim, use_softmax=self.use_softmax, apply_log=self.loss == 'nll_log')
        if self.model_weights_path is not '':
            if self.verbose:
                print(
                    f'Training model with weights of file {self.model_weights_path}')
            if pretrained_basic and not basic_only:
                model.basic_network.load_state_dict(
                    torch.load(self.model_weights_path))
            else:
                model.load_state_dict(torch.load(self.model_weights_path))
        model.to(self.device)

        return model

    def _save_model(self, epoch, model, return_f1=False, f1=0.0, early_stopping=False):
        if self.save_at is not None and self.save_path_head is not None and self.save_params is not None:
            if epoch in self.save_at or early_stopping:
                params = self.save_params
                if return_f1:
                    path = get_model_path(
                        self.save_path_head, params['stem'], params['current_time'], params['hyperparams'], f1)
                else:
                    path = get_model_path(
                        self.save_path_head, params['stem'], params['current_time'], params['hyperparams'])
                path += f'_epoch{epoch}'
                if early_stopping:
                    path += f'_early_stopping'
                path += '.pt'

                print(f'Saving model at: {path}')
                torch.save(model.state_dict(), path)

    def _create_pseudo_labels(self):
        model = BasicNetwork(self.embedding_dim,
                             self.label_dim, use_softmax=self.use_softmax)
        for pseudo_ann in self.pseudo_annotators:
            model.load_state_dict(torch.load(self.pseudo_model_path_func(
                **self.pseudo_func_args, annotator=pseudo_ann)))
            model.to(self.device)
            annotator_list = self.dataset.annotators.copy()
            annotator_list.remove(pseudo_ann)
            for annotator in annotator_list:
                self.dataset.create_pseudo_labels(annotator, pseudo_ann, model)

    def initialize_optimizer(self, parameters):
        if self.optimizer_name == 'adam':
            return optim.AdamW(
                parameters,
                lr=self.learning_rate, betas=(0.9, 0.999), eps=1e-08, weight_decay=0.01, amsgrad=False)
        elif self.optimizer_name == 'sgd':
            return optim.SGD(
                parameters,
                lr=self.learning_rate, momentum=0.9, weight_decay=0.0005)

    def _print(self, *args, **kwargs):

        print(*args, **kwargs)

    def fit(self, epochs, return_f1=False, single_annotator=None, basic_only=False, fix_base=False,
            pretrained_basic=False, deep_randomization=False, early_stopping_interval=0):
        model = self._get_model(basic_only=basic_only,
                                pretrained_basic=pretrained_basic)
        if single_annotator is not None or basic_only:
            self.annotator_dim = 1
            optimizer = self.initialize_optimizer(model.parameters())

        if self.loss == 'bce':
            criterion = nn.BCELoss()
        elif self.loss == 'nll' or self.loss == 'nll_log':
            criterion = nn.NLLLoss()
        elif self.loss == 'cross':
            criterion = nn.CrossEntropyLoss()
        if single_annotator is None and not basic_only:
            if not fix_base:
                optimizer = self.initialize_optimizer(model.parameters())
            else:
                optimizer = self.initialize_optimizer(
                    model.bias_matrices.parameters())

        loss_history = []
        if early_stopping_interval is not 0:
            val_mean_losses = []
        inputs = 0
        labels = 0

        # self._print('START TRAINING')
        if self.verbose:
            self._print(
                f'learning rate: {self.learning_rate} - batch size: {self.batch_size}')
        for epoch in range(epochs):
            f1 = 0.0
            samples_looked_at = 0.0

            if deep_randomization:
                if single_annotator is not None:
                    self.dataset.set_annotator_filter(single_annotator)
                    annotators = [single_annotator]
                else:
                    self.dataset.no_annotator_filter()
                    annotators = self.dataset.annotators

                self.dataset.data_shuffle_after_split()

                # training
                self.dataset.set_mode('train')
                train_loader = torch.utils.data.DataLoader(
                    self.dataset, batch_size=self.batch_size, collate_fn=self.collate_wrapper, shuffle=True)
                self.fit_epoch_deep_randomization(model, optimizer, criterion, train_loader, epoch, loss_history,
                                                  annotators=annotators, basic_only=basic_only)
                # validation
                self.dataset.set_mode('validation')
                if len(self.dataset) is 0:
                    self.dataset.set_mode('train')
                val_loader = torch.utils.data.DataLoader(
                    self.dataset, batch_size=self.batch_size, collate_fn=self.collate_wrapper, shuffle=True)
                val_loss, _, f1 = self.fit_epoch_deep_randomization(model, optimizer, criterion, val_loader, epoch,
                                                                    loss_history, annotators=annotators,
                                                                    basic_only=basic_only, mode='validation', return_metrics=return_f1)
                if f1 is not None and isinstance(f1, dict):
                    f1_temp = 0.0
                    for ann in self.dataset.annotators:
                        f1_temp += f1[ann]['score'] * f1[ann]['samples']
                    f1 = f1_temp / sum([f1[ann]['samples'] for ann in self.dataset.annotators])

                if val_loss is not None and isinstance(val_loss, dict):
                    val_loss_temp = 0.0
                    for ann in self.dataset.annotators:
                        val_loss_temp += val_loss[ann]['score'] * val_loss[ann]['samples']
                    val_loss = val_loss_temp / sum([val_loss[ann]['samples'] for ann in self.dataset.annotators])

            else:
                # loop over all annotators
                for i in range(self.annotator_dim):
                    # switch to current annotator
                    if single_annotator is not None:
                        annotator = single_annotator
                        self.dataset.set_annotator_filter(annotator)
                        no_annotator_head = True
                    elif single_annotator is None and basic_only:
                        annotator = 'all'
                        self.dataset.no_annotator_filter()
                        no_annotator_head = True
                    else:
                        annotator = self.dataset.annotators[i]
                        self.dataset.set_annotator_filter(annotator)
                        no_annotator_head = False

                    # training
                    self.dataset.set_mode('train')
                    train_loader = torch.utils.data.DataLoader(
                        self.dataset, batch_size=self.batch_size, collate_fn=self.collate_wrapper)
                    self.fit_epoch(model, optimizer, criterion, train_loader, annotator, i,
                                   epoch, loss_history, no_annotator_head=no_annotator_head)

                    # validation
                    self.dataset.set_mode('validation')
                    val_loader = torch.utils.data.DataLoader(
                        self.dataset, batch_size=self.batch_size, collate_fn=self.collate_wrapper)
                    if return_f1:
                        if len(val_loader) is 0:
                            self.dataset.set_mode('train')
                            val_loader = torch.utils.data.DataLoader(
                                self.dataset, batch_size=self.batch_size, collate_fn=self.collate_wrapper)
                        val_loss, _, f1_ann = self.fit_epoch(model, optimizer, criterion, val_loader, annotator, i,
                                                             epoch, loss_history, mode='validation', return_metrics=True,
                                                             no_annotator_head=no_annotator_head)
                        # essentially micro averaging across annotators
                        f1 = (samples_looked_at * f1 + f1_ann * len(self.dataset)
                              ) / (samples_looked_at + len(self.dataset))
                        samples_looked_at += len(self.dataset)
                        # print(f'DEBUG SOLVER - i: {i}, f1: {f1}, f1_ann: {f1_ann}, samples_looked_at: {samples_looked_at},
                        # len dataset: {len(self.dataset)}')
                    else:
                        self.fit_epoch(model, optimizer, criterion, val_loader, annotator, i,
                                       epoch, loss_history, mode='validation', no_annotator_head=no_annotator_head)

            self._save_model(epoch, model, return_f1=return_f1, f1=f1)

            # if mean loss doesn't change over several epochs, stop early with training
            if early_stopping_interval is not 0:
                val_mean_losses.append(val_loss)
                if len(val_mean_losses) > early_stopping_interval:
                    loss_begin = val_mean_losses[-early_stopping_interval]
                    stop_margin_step = self.early_stopping_margin * loss_begin
                    stop_margins = [loss_begin - stop_margin_step,
                                    loss_begin + stop_margin_step]
                    losses_interval = val_mean_losses[-early_stopping_interval:]
                    mean_loss_interval = sum(
                        losses_interval) / len(losses_interval)
                    if mean_loss_interval > stop_margins[0] and mean_loss_interval < stop_margins[1]:
                        self._print(f'Stopping early at epoch {epoch} with loss {mean_loss_interval}')
                        self._save_model(epoch, model, return_f1=return_f1, f1=f1, early_stopping=True)
                        if return_f1:
                            return model, f1
                        else:
                            return model

        if self.verbose:
            self._print('Finished Training' + 20 * ' ')
            self._print('sum of first 10 losses: ', sum(loss_history[0:10]))
            self._print('sum of last  10 losses: ', sum(loss_history[-10:]))

        if return_f1:
            return model, f1

        return model

    def fit_epoch(self, model, opt, criterion, data_loader, annotator, annotator_idx, epoch, loss_history, mode='train',
                  return_metrics=False, no_annotator_head=False):
        if no_annotator_head:
            annotator_idx = None
        mean_loss = 0.0
        mean_accuracy = 0.0
        mean_precision = 0.0
        mean_recall = 0.0
        mean_f1 = 0.0
        len_data_loader = len(data_loader)
        for i, data in enumerate(data_loader, 1):
            # Prepare inputs to be passed to the model
            # Prepare labels for the Loss computation
            self._print(
                f'Annotator {annotator} - Epoch {epoch}: Step {i} / {len_data_loader}' + 10 * ' ', end='\r')
            inputs, labels, pseudo_labels = data.input, data.target, data.pseudo_targets
            opt.zero_grad()

            # Generate predictions
            if annotator_idx is not None:
                outputs = model(inputs)[annotator_idx]
                if len(pseudo_labels) is not 0:
                    outputs_pseudo_labels = model(inputs)
                    opt = self.initialize_optimizer(model.parameters())
                    if isinstance(pseudo_labels, list):
                        pseudo_annotators = set(
                            [ann for sample in pseudo_labels for ann in list(sample.keys())])
                        losses = [criterion(outputs_pseudo_labels[self.dataset.annotators.index(ann)].float(),
                                            torch.tensor([sample[ann] for sample in pseudo_labels]).to(device=self.device))
                                  for ann in pseudo_annotators]
                    else:
                        losses = [criterion(outputs_pseudo_labels[self.dataset.annotators.index(ann)].float(), pseudo_labels[ann])
                                  for ann in pseudo_labels.keys()]
            else:
                outputs = model(inputs)

            # Compute Loss:
            loss = criterion(outputs.float(), labels)

            # performance measures of the batch
            predictions = outputs.argmax(dim=1)
            accuracy, precision, recall, f1 = self.performance_measures(
                predictions, labels, self.averaging_method)

            # statistics for logging
            current_batch_size = inputs.shape[0]
            divisor = (i - 1) * self.batch_size + current_batch_size
            mean_loss = ((i - 1) * self.batch_size * mean_loss +
                         loss.item() * current_batch_size) / divisor
            mean_accuracy = (mean_accuracy * self.batch_size * (i - 1) +
                             accuracy.item() * current_batch_size) / divisor
            mean_precision = (mean_precision * self.batch_size *
                              (i - 1) + precision.item() * current_batch_size) / divisor
            mean_recall = (mean_recall * self.batch_size * (i - 1) +
                           recall.item() * current_batch_size) / divisor
            mean_f1 = (mean_f1 * self.batch_size * (i - 1) +
                       f1.item() * current_batch_size) / divisor
            loss_history.append(loss.item())

            if mode is 'train':
                # Update gradients
                if annotator_idx is not None and len(pseudo_labels) is not 0:
                    final_loss = loss
                    for pseudo_loss in losses:
                        final_loss += pseudo_loss
                    final_loss.backward()
                else:
                    loss.backward()

                # Optimization step
                opt.step()

            if self.writer is not None:
                self.writer.add_scalar(
                    f'Loss/Annotator {annotator}/{mode}', mean_loss, epoch)
                self.writer.add_scalar(
                    f'Accuracy/Annotator {annotator}/{mode}', mean_accuracy, epoch)
                self.writer.add_scalar(
                    f'Precision/Annotator {annotator}/{mode}', mean_precision, epoch)
                self.writer.add_scalar(
                    f'Recall/Annotator {annotator}/{mode}', mean_recall, epoch)
                self.writer.add_scalar(
                    f'F1 score/Annotator {annotator}/{mode}', mean_f1, epoch)

        if return_metrics:
            return mean_loss, mean_accuracy, mean_f1

    def fit_epoch_deep_randomization(self, model, optimizer, criterion, data_loader, epoch, loss_history, annotators=[], mode='train',
                                     return_metrics=False, basic_only=False):
        """
            this function is made to take a dataset with no annotation filter
            and thus having batches with samples by different annotators. It also
            works with annotation filters. (normal fit_epoch function is more efficient
            though for this purpose)
        """
        # Setup
        if basic_only:
            single_annotator = None
            if len(annotators) is 1:
                single_annotator = annotators[0]
            annotators = []
            mean_loss = 0.0
            mean_accuracy = 0.0
            mean_precision = 0.0
            mean_recall = 0.0
            mean_f1 = 0.0
        else:
            if len(annotators) == 0:
                print('ERROR - Please provide annotators in correct order!')
                return
            mean_loss = {ann: {'score': 0.0, 'samples': 0.0} for ann in annotators}
            mean_accuracy = {ann: {'score': 0.0, 'samples': 0.0} for ann in annotators}
            mean_precision = {ann: {'score': 0.0, 'samples': 0.0} for ann in annotators}
            mean_recall = {ann: {'score': 0.0, 'samples': 0.0} for ann in annotators}
            mean_f1 = {ann: {'score': 0.0, 'samples': 0.0} for ann in annotators}

        if self.loss == 'bce':
            one_hot = torch.eye(self.label_dim).to(self.device)

        # Training loop
        len_data_loader = len(data_loader)
        for i, data in enumerate(data_loader, 1):
            inputs, labels, pseudo_labels, annotations = data.input, data.target, data.pseudo_targets, data.annotations
            optimizer.zero_grad()

            # Generate predictions
            losses = {}
            if len(pseudo_labels) is not 0:
                pseudo_annotators = set(
                    [ann for sample in pseudo_labels for ann in list(sample.keys())])

            for annotator_idx, annotator in enumerate(annotators):
                self._print(
                    f'Epoch {epoch}: Step {i} / {len_data_loader}  -  Annotator {annotator}' + 10 * ' ', end='\r')
                # desired_ann = 'male'
                # log_path = '../logs/train_11_01/bias_inspection.txt'
                # if i % 5 == 0:           # in [int(0.2 * len_data_loader), int(0.4 * len_data_loader), int(0.6 * len_data_loader),
                #     #  int(0.8 * len_data_loader), len_data_loader - 1]:
                #     labels_strings = ['neg', 'pos']
                #     bias_out = f'Bias matrix at step {i} - annotator {annotator}:'
                #     bias_out += '\t' + f'{labels_strings[0]}' + '\t ' * 3 + f'{labels_strings[1]}\n'
                #     for j, labels_string in enumerate(labels_strings):
                #         bias_out += f'{labels_string}' + ' ' * (25 - len(labels_string))
                #         for k, labels_string_2 in enumerate(labels_strings):
                #             bias_out += '\t' * 3 + f'{model.bias_matrices[annotator_idx].weight[j][k].cpu().detach().numpy(): .8f}'
                #         bias_out += '\n'
                #     bias_out += '\n'
                #     with open(log_path, 'a') as f:
                #         f.write(bias_out)
                #     time.sleep(1)

                outputs = model(inputs)
                outputs_annotator = outputs[annotator_idx]
                loss_annotations = None

                optimizer = self.initialize_optimizer(model.parameters())

                if annotator in set(annotations):
                    mask_labels = torch.tensor(
                        [ann == annotator for ann in annotations]).to(device=self.device)
                    mask_outputs = torch.tensor(
                        [[ann == annotator] * self.label_dim for ann in annotations]).to(device=self.device)
                    labels_annotations = torch.masked_select(
                        labels, mask_labels)
                    labels_annotations_for_performance = labels_annotations.detach().clone()
                    outputs_dim = (
                        mask_labels[mask_labels].shape[0], outputs_annotator.shape[1])
                    outputs_annotations = torch.masked_select(
                        outputs_annotator, mask_outputs).reshape(outputs_dim)
                    if self.loss == 'bce':
                        # one hot encode for bce loss
                        labels_annotations = one_hot[labels_annotations]
                    loss_annotations = criterion(
                        outputs_annotations.float(), labels_annotations)

                # search for this annotator in pseudo labels of samples by all other annotators to add the losses
                loss_pseudo_annotations = None
                if len(pseudo_labels) is not 0:
                    if annotator in pseudo_annotators:
                        mask_labels = [annotator in list(
                            sample.keys()) for sample in pseudo_labels]
                        labels_pseudo_annotations = torch.tensor([int(sample[annotator])
                                                                  for sample in compress(pseudo_labels, mask_labels)]).to(device=self.device)
                        mask_outputs = torch.tensor([[annotator in list(sample.keys())] * self.label_dim
                                                     for sample in pseudo_labels]).to(device=self.device)
                        outputs_dim = (
                            len([x for x in compress(mask_labels, mask_labels)]), outputs_annotator.shape[1])
                        outputs_pseudo_annotations = torch.masked_select(
                            outputs_annotator, mask_outputs).reshape(outputs_dim)
                        if self.loss == 'bce':
                            # one hot encode for bce loss
                            labels_pseudo_annotations = one_hot[labels_pseudo_annotations]
                        loss_pseudo_annotations = criterion(
                            outputs_pseudo_annotations.float(), labels_pseudo_annotations)

                if loss_annotations is not None or loss_pseudo_annotations is not None:
                    if loss_annotations is not None and loss_pseudo_annotations is not None:
                        loss = loss_annotations + loss_pseudo_annotations
                    elif loss_annotations is not None and loss_pseudo_annotations is None:
                        loss = loss_annotations
                    elif loss_pseudo_annotations is not None and loss_annotations is None:
                        loss = loss_pseudo_annotations

                    if annotator in set(annotations):
                        # record performance for this annotator (discard pseudo annotations)
                        predictions = outputs_annotations.argmax(dim=1)
                        accuracy, precision, recall, f1 = self.performance_measures(
                            predictions, labels_annotations_for_performance, self.averaging_method)

                        # statistics for logging
                        current_batch_size = predictions.shape[0]
                        mean_loss[annotator]['score'] = (mean_loss[annotator]['score'] * mean_loss[annotator]['samples'] +
                                                         loss.item() * current_batch_size) / (mean_loss[annotator]['samples'] + current_batch_size)
                        mean_accuracy[annotator]['score'] = (mean_accuracy[annotator]['score'] * mean_accuracy[annotator]['samples'] +
                                                             accuracy.item() * current_batch_size) / (mean_accuracy[annotator]['samples']
                                                                                                      + current_batch_size)
                        mean_precision[annotator]['score'] = (mean_precision[annotator]['score'] * mean_precision[annotator]['samples'] +
                                                              precision.item() * current_batch_size) / (mean_precision[annotator]['samples']
                                                                                                        + current_batch_size)
                        mean_recall[annotator]['score'] = (mean_recall[annotator]['score'] * mean_recall[annotator]['samples']
                                                           + recall.item() * current_batch_size) / (mean_recall[annotator]['samples']
                                                                                                    + current_batch_size)
                        mean_f1[annotator]['score'] = (
                            mean_f1[annotator]['score'] * mean_f1[annotator]['samples'] + f1.item() * current_batch_size) \
                            / (mean_f1[annotator]['samples'] + current_batch_size)
                        # update sample counter for current annotator
                        mean_loss[annotator]['samples'] += current_batch_size
                        mean_accuracy[annotator]['samples'] += current_batch_size
                        mean_precision[annotator]['samples'] += current_batch_size
                        mean_recall[annotator]['samples'] += current_batch_size
                        mean_f1[annotator]['samples'] += current_batch_size
                        loss_history.append(loss.item())

                        if self.writer is not None:
                            self.writer.add_scalar(
                                f'Loss/Annotator {annotator}/{mode}', mean_loss[annotator]['score'], epoch)
                            self.writer.add_scalar(
                                f'Accuracy/Annotator {annotator}/{mode}', mean_accuracy[annotator]['score'], epoch)
                            self.writer.add_scalar(
                                f'Precision/Annotator {annotator}/{mode}', mean_precision[annotator]['score'], epoch)
                            self.writer.add_scalar(
                                f'Recall/Annotator {annotator}/{mode}', mean_recall[annotator]['score'], epoch)
                            self.writer.add_scalar(
                                f'F1 score/Annotator {annotator}/{mode}', mean_f1[annotator]['score'], epoch)

                    if mode is 'train':
                        # Update gradients
                        if loss_annotations is not None:
                            retain_graph = False
                            if loss_pseudo_annotations is not None:
                                retain_graph = True
                            loss_annotations.backward(
                                retain_graph=retain_graph)
                        if loss_pseudo_annotations is not None:
                            loss_pseudo_annotations.backward()

                        # Optimization step
                        # print(f'These are the parameters in optimizer: {optimizer}')
                        # print(f'Bias matrix weights before: {model.bias_matrices[annotator_idx].weight}')
                        optimizer.step()
                        # print(f'Bias matrix weights after: {model.bias_matrices[annotator_idx].weight}')
                        # if annotator == 'male':
                        #     self.optimizer, self.model = optimizer, model
                        # sys.exit()
                        optimizer.zero_grad()

            if basic_only:
                annotator = 'all'
                if single_annotator is not None:
                    annotator = single_annotator
                self._print(
                    f'Annotator {annotator} - Epoch {epoch}: Step {i} / {len_data_loader}' + 10 * ' ', end='\r')
                outputs = model(inputs)

                labels_for_performance = labels.detach().clone()

                if self.loss == 'bce':
                    # one hot encode for bce loss
                    labels = one_hot[labels]

                # Pseudo Labels:
                if len(pseudo_labels) is not 0:
                    loss_pseudo_annotations = []
                    for ann in pseudo_annotators:
                        mask_labels = [ann in list(
                            sample.keys()) for sample in pseudo_labels]
                        labels_pseudo_annotations = torch.tensor([int(sample[ann])
                                                                  for sample in compress(pseudo_labels, mask_labels)]).to(device=self.device)
                        mask_outputs = torch.tensor([[ann in list(sample.keys())] * self.label_dim
                                                     for sample in pseudo_labels]).to(device=self.device)
                        outputs_dim = (
                            len([x for x in compress(mask_labels, mask_labels)]), outputs.shape[1])
                        outputs_pseudo_annotations = torch.masked_select(
                            outputs, mask_outputs).reshape(outputs_dim)
                        if self.loss == 'bce':
                            # one hot encode for bce loss
                            labels_pseudo_annotations = one_hot[labels_pseudo_annotations]
                        loss_pseudo_annotations.append(criterion(
                            outputs_pseudo_annotations.float(), labels_pseudo_annotations))

                # Compute Loss:
                loss = criterion(outputs.float(), labels)

                # performance measures of the batch
                predictions = outputs.argmax(dim=1)
                accuracy, precision, recall, f1 = self.performance_measures(
                    predictions, labels_for_performance, self.averaging_method)

                # statistics for logging
                current_batch_size = inputs.shape[0]
                divisor = (i - 1) * self.batch_size + current_batch_size
                mean_loss = ((i - 1) * self.batch_size * mean_loss +
                             loss.item() * current_batch_size) / divisor
                mean_accuracy = (mean_accuracy * self.batch_size * (i - 1) +
                                 accuracy.item() * current_batch_size) / divisor
                mean_precision = (mean_precision * self.batch_size *
                                  (i - 1) + precision.item() * current_batch_size) / divisor
                mean_recall = (mean_recall * self.batch_size * (i - 1) +
                               recall.item() * current_batch_size) / divisor
                mean_f1 = (mean_f1 * self.batch_size * (i - 1) +
                           f1.item() * current_batch_size) / divisor
                loss_history.append(loss.item())

                if mode is 'train':
                    # Update gradients
                    if loss is not None:
                        retain_graph = False
                        if len(loss_pseudo_annotations) is not 0:
                            retain_graph = True
                        loss.backward(
                            retain_graph=retain_graph)
                    if len(loss_pseudo_annotations) is not 0:
                        for loss_pseudo in loss_pseudo_annotations:
                            loss_pseudo.backward(retain_graph=retain_graph)

                    # Optimization step
                    optimizer.step()

                if self.writer is not None:
                    self.writer.add_scalar(
                        f'Loss/Annotator {annotator}/{mode}', mean_loss, epoch)
                    self.writer.add_scalar(
                        f'Accuracy/Annotator {annotator}/{mode}', mean_accuracy, epoch)
                    self.writer.add_scalar(
                        f'Precision/Annotator {annotator}/{mode}', mean_precision, epoch)
                    self.writer.add_scalar(
                        f'Recall/Annotator {annotator}/{mode}', mean_recall, epoch)
                    self.writer.add_scalar(
                        f'F1 score/Annotator {annotator}/{mode}', mean_f1, epoch)

        if return_metrics:
            return mean_loss, mean_accuracy, mean_f1

    def evaluate_model(self, output_file_path, labels=None, mode='train', pretrained_basic_path='', basic_only=False):
        model = self._get_model(basic_only=basic_only)

        # load pretrained model for comparison
        if pretrained_basic_path != '':
            pretrained_model = BasicNetwork(
                self.embedding_dim, self.label_dim, use_softmax=self.use_softmax)
            pretrained_model.load_state_dict(torch.load(pretrained_basic_path))
            pretrained_model.to(self.device)

        # also document loss
        if self.loss == 'bce':
            one_hot = torch.eye(self.label_dim).to(self.device)
            criterion = nn.BCELoss()
        elif self.loss == 'nll' or self.loss == 'nll_log':
            criterion = nn.NLLLoss()
        elif self.loss == 'cross':
            criterion = nn.CrossEntropyLoss()
        mean_loss = 0.0
        annotators_mean_losses = {}

        # write annotation bias matrices into log file
        import sys
        original_stdout = sys.stdout
        with open(output_file_path, 'w') as f:
            sys.stdout = f
            self.dataset.set_mode(mode)
            out_text = ''
            acc_out = ''
            bias_conf_out = 'Annotation bias and confusion matrices\n\n'
            overall_correct = 0
            overall_len = 0
            if pretrained_basic_path != '':
                pretrained_correct = 0
            for ann_idx, annotator in enumerate(self.dataset.annotators):
                correct = {ann: 0 for ann in self.dataset.annotators}

                different_answers = 0
                different_answers_idx = []

                annotators_mean_losses[annotator] = 0.0

                confusion_matrix = np.zeros((self.label_dim, self.label_dim))

                self.dataset.set_annotator_filter(annotator)
                # batch_size needs to be 1
                data_loader = torch.utils.data.DataLoader(
                    self.dataset, batch_size=1, collate_fn=self.collate_wrapper)

                for sample_idx, data in enumerate(data_loader, 1):
                    # Prepare inputs to be passed to the model
                    inp, label = data.input, data.target

                    # Generate predictions
                    if basic_only:
                        latent_truth = model(inp)
                    else:
                        latent_truth = model.basic_network(inp)
                    output = model(inp)
                    if pretrained_basic_path != '':
                        pretrained_output = pretrained_model(inp)

                    # calculate loss
                    if self.loss == 'bce':
                        # one hot encode for bce loss
                        label = one_hot[label]

                    if not basic_only:
                        annotators_mean_losses[annotator] += criterion(
                            output[ann_idx].float(), label).item()

                        # generate confusion matrix
                        confusion_matrix[label.cpu().numpy().item(0)][latent_truth.argmax(
                            dim=1).cpu().detach().numpy().item(0)] += 1.0

                        # print input/output
                        out_text += f'Point {sample_idx} - Label by {annotator}: {label.cpu().numpy()} - Latent truth {latent_truth.cpu().detach().numpy()}'
                        predictions = {}
                        for idx, ann in enumerate(self.dataset.annotators):
                            out_text += f' - Annotator {ann} {output[idx].cpu().detach().numpy()}'
                            predictions[ann] = [output[idx].argmax(
                                dim=1), output[idx].max(dim=1)]

                        # compare the prediction with the label for each annotator
                        for idx, ann in enumerate(self.dataset.annotators):
                            if predictions[ann][0] == label:
                                # annotator_highest_pred = max(filtered_preds, key=filtered_preds.get)
                                correct[ann] += 1

                        # compare the prediction with the label for the annotator that created the label
                        if predictions[annotator][0] == label:
                            overall_correct += 1

                        predictions_set = set([predictions[ann][0].cpu().detach(
                        ).numpy().item(0) for ann in self.dataset.annotators])
                        if len(predictions_set) is not 1:
                            different_answers += 1
                            different_answers_idx.append(sample_idx)

                    else:
                        if output.argmax(dim=1) == label:
                            overall_correct += 1

                    # compare the prediction with the label for the pretrained model
                    if pretrained_basic_path != '':
                        if pretrained_output.argmax(dim=1) == label:
                            pretrained_correct += 1

                    out_text += '\n'

                if not basic_only:
                    if len(self.dataset) != 0:
                        annotators_mean_losses[annotator] /= len(self.dataset)
                    mean_loss += annotators_mean_losses[annotator]

                    for row in confusion_matrix:
                        if row.sum() != 0.0:
                            row /= row.sum()

                    bias_conf_out += f'Annotator {annotator}\n'
                    bias_conf_out += f'Output\\LatentTruth'
                    if labels is not None:
                        for label in labels:
                            bias_conf_out += '\t' * 3 + f'{label}'
                        bias_conf_out += '\t' * 5 + f'Label\\LatentTruth'
                        for label in labels:
                            bias_conf_out += '\t' * 3 + f'{label}'
                        bias_conf_out += '\n'
                        for j, label in enumerate(labels):
                            bias_conf_out += f'{label}' + ' ' * (15 - len(label))
                            for k, label_2 in enumerate(labels):
                                bias_conf_out += '\t' * 3 + \
                                    f'{model.bias_matrices[ann_idx].weight[j][k].cpu().detach().numpy(): .4f}'
                            bias_conf_out += '\t' * 5
                            bias_conf_out += f'{label}' + ' ' * (15 - len(label))
                            for k, label_2 in enumerate(labels):
                                bias_conf_out += '\t' * 3 + \
                                    f'{confusion_matrix[j][k]: .4f}'
                            bias_conf_out += '\n'
                        bias_conf_out += '\n'
                    else:
                        bias_conf_out += f'{model.bias_matrices[ann_idx].weight.cpu().detach().numpy()}\n\n'

                overall_len += len(self.dataset)

                # Document correct predictions
                all_same_accuracies = set([correct[ann]
                                           for ann in self.dataset.annotators])
                acc_out += '-' * 25 + \
                    f'   Annotator {annotator}   ' + '-' * 25 + '\n'
                acc_out += f'Different answers given by bias matrices {different_answers} / {len(self.dataset)} times\n'
                acc_out += f'Different answers at points: {different_answers_idx[:min(5, len(different_answers_idx))]}\n'
                acc_out += f'Accuracies of samples labeled by {annotator}:'
                # ' has {len(all_same_accuracies)} '
                # if len(all_same_accuracies) is 1:
                #     acc_out += 'answer\n'
                # else:
                #     acc_out += 'different answers\n'
                acc_out += '\n'
                for ann in self.dataset.annotators:
                    acc_out += f'Annotator {ann}: {correct[ann]} / {len(self.dataset)}     '
                acc_out += '\n\n'

            # Document Loss
            mean_loss /= len(self.dataset.annotators)
            loss_out = f'Mean Loss (over annotators & samples): {mean_loss:.5f}\n'
            loss_out += f'Mean Loss for each annotator (over samples):\n'
            for ann in self.dataset.annotators:
                loss_out += f'Annotator {ann}: {annotators_mean_losses[ann]:.5f}        '
            loss_out += '\n\n\n'

            # Document overall accuracy
            overall_acc_out = 'Overall accuracies\n\n'
            overall_accuracy = overall_correct / overall_len
            overall_acc_out += f'Accuracy after extensive training'
            if not basic_only:
                overall_acc_out += ' with bias matrices'
            overall_acc_out += f': {overall_correct} / {overall_len} or as percentage: {overall_accuracy:.5f}\n'
            if pretrained_basic_path != '':
                pretrained_accuracy = pretrained_correct / overall_len
                overall_acc_out += f'Accuracy with pretrained model: {pretrained_correct} / {overall_len} ' + \
                    f'or as percentage: {pretrained_accuracy:.5f}\n\n'
            else:
                overall_acc_out += '\n\n'

            print(overall_acc_out)
            if not basic_only:
                print(loss_out)
                print(bias_conf_out)
                print(acc_out)
                print('\n' * 30)
                print(out_text)
            sys.stdout = original_stdout

    def evaluate_model_simple(self, labeling_scheme='single', pretrained_basic_path='', basic_only=False, mode='test',
                              return_metrics=True, averaging_method='macro'):
        """
        Calculate accuracy and f1 score for model and pretrained model.
        Apply Dawid-Skene or Majority Voting to get ground truth labels for evaluation.
        Alternatively only evaluate on data of one annotator (for tripadvisor dataset)
        In the end, the goal of IPA2LT is to train a basic classifier and only use bias matrices
        to capture bias and reduce noise.
        """
        # how many labels per sample (only LTNet should be evaluated with multi)
        if labeling_scheme not in ['single', 'multi']:
            print('Wrong labeling scheme! Needs to be single or multi.')
            return

        model = self._get_model(basic_only=basic_only)
        self.dataset.set_mode(mode)

        # load pretrained model for comparison
        if pretrained_basic_path != '':
            pretrained_model = BasicNetwork(
                self.embedding_dim, self.label_dim, use_softmax=self.use_softmax)
            pretrained_model.load_state_dict(torch.load(pretrained_basic_path))
            pretrained_model.to(self.device)

        # init
        accuracy, pretrained_accuracy, f1, pretrained_f1 = 0.0, 0.0, 0.0, 0.0

        if labeling_scheme == 'single':
            if not basic_only:
                # only consider basic classifier of LTNet
                model = model.basic_network

            # batch_size needs to be 1
            data_loader = torch.utils.data.DataLoader(
                self.dataset, batch_size=1, collate_fn=self.collate_wrapper)

            # to calculate the f1 score, we need all predictions and labels by one annotator
            predictions = []
            pretrained_predictions = []
            labels = []

            for sample_idx, data in enumerate(data_loader, 1):
                # Prepare inputs to be passed to the model
                inp, label = data.input, data.target

                # Generate predictions
                output = model(inp)
                if pretrained_basic_path != '':
                    pretrained_output = pretrained_model(inp)

                predictions.append(output.argmax(dim=1).item())
                labels.append(label.item())

                # document the prediction of the pretrained model
                if pretrained_basic_path != '':
                    pretrained_predictions.append(pretrained_output.argmax(dim=1))

            # calculate metrics for annotator
            accuracy, _, _, f1 = self.performance_measures(torch.tensor(predictions), torch.tensor(labels), averaging_method=averaging_method)
            # print(f'DEBUG\nAccuracy {accuracy}   -   F1 {f1}\nPredictions+Labels\n{[[x, y] for x, y in zip(labels, predictions)]}')
            if pretrained_basic_path != '':
                pretrained_accuracy, _, _, pretrained_f1 = self.performance_measures(torch.tensor(
                    pretrained_predictions), torch.tensor(labels), averaging_method=averaging_method)

        if labeling_scheme == 'mutli':
            overall_len = 0
            annotator_lens = {}
            metrics = {}
            pretrained_metrics = {}
            if pretrained_basic_path != '':
                pretrained_correct = 0
            for ann_idx, annotator in enumerate(self.dataset.annotators):
                self.dataset.set_annotator_filter(annotator)
                # batch_size needs to be 1
                data_loader = torch.utils.data.DataLoader(
                    self.dataset, batch_size=1, collate_fn=self.collate_wrapper)

                # to calculate the f1 score, we need all predictions and labels by one annotator
                predictions = []
                pretrained_predictions = []
                labels = []

                for sample_idx, data in enumerate(data_loader, 1):
                    # Prepare inputs to be passed to the model
                    inp, label = data.input, data.target

                    # Generate predictions
                    if basic_only:
                        output = model(inp)
                    else:
                        output = model(inp)[ann_idx]
                    if pretrained_basic_path != '':
                        pretrained_output = pretrained_model(inp)

                    predictions.append(output.argmax(dim=1).item())
                    labels.append(label.item())

                    # document the prediction of the pretrained model
                    if pretrained_basic_path != '':
                        pretrained_predictions.append(pretrained_output.argmax(dim=1))

                overall_len += len(self.dataset)
                annotator_lens[annotator] = len(self.dataset)

                # calculate metrics for annotator
                metrics[annotator] = self.performance_measures(torch.tensor(predictions), torch.tensor(labels), averaging_method=averaging_method)
                pretrained_metrics[annotator] = self.performance_measures(torch.tensor(
                    pretrained_predictions), torch.tensor(labels), averaging_method=averaging_method)

            # average over annotator metrics with weights relative to number of samples
            accuracy = sum([metrics[ann][0] * annotator_lens[ann] for ann in self.dataset.annotators]) / overall_len
            pretrained_accuracy = sum([pretrained_metrics[ann][0] * annotator_lens[ann] for ann in self.dataset.annotators]) / overall_len
            f1 = sum([metrics[ann][3] * annotator_lens[ann] for ann in self.dataset.annotators]) / overall_len
            pretrained_f1 = sum([pretrained_metrics[ann][3] * annotator_lens[ann] for ann in self.dataset.annotators]) / overall_len

        return accuracy, pretrained_accuracy, f1, pretrained_f1

    @staticmethod
    def performance_measures(predictions, labels, averaging_method='macro'):
        if predictions.device.type == 'cuda' or labels.device.type == 'cuda':
            predictions, labels = predictions.cpu(), labels.cpu()

        # averaging for multiclass targets, can be one of [â€˜microâ€™, â€˜macroâ€™, â€˜samplesâ€™, â€˜weightedâ€™]
        accuracy = accuracy_score(labels, predictions)
        zero_division = 0
        precision = precision_score(
            labels, predictions, average=averaging_method, zero_division=zero_division)
        recall = recall_score(
            labels, predictions, average=averaging_method, zero_division=zero_division)
        f1 = f1_score(labels, predictions, average=averaging_method,
                      zero_division=zero_division)

        return accuracy, precision, recall, f1
