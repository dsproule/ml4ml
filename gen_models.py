import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
import json
from qkeras import QDense, QConv2D, QConv1D, QAveragePooling2D, QActivation, quantized_bits, QDepthwiseConv2D, QSeparableConv2D, QSeparableConv1D, QLSTM
from keras.layers import Dense, Conv2D, Flatten, Activation, Conv1D, LSTM, Layer, Input
from keras.models import Model, model_from_json
# from qkeras.utils import _add_supported_quantized_objects
import keras
import typing
import random
import time
import numpy as np
from tqdm import tqdm
from contextlib import contextmanager
import sys, os
import ray
from ray.exceptions import RayTaskError
from qkeras.quantizers import quantized_bits
from qkeras import Clip

import logging

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
@contextmanager
def suppress_stdout():
    with open(os.devnull, "w") as devnull:
        old_stdout = sys.stdout
        sys.stdout = devnull
        try:
            yield
        finally:
            sys.stdout = old_stdout

clip_base_2 = lambda x: 2 ** round(np.log2(x))

def _add_supported_quantized_objects(co: dict):

    co.update({
        "QDense": QDense,
        "QConv2D": QConv2D,
        "QConv1D": QConv1D,
        "QActivation": QActivation,
        "QAveragePooling2D": QAveragePooling2D,
        "QDepthwiseConv2D": QDepthwiseConv2D,
        "QSeparableConv2D": QSeparableConv2D,
        "QSeparableConv1D": QSeparableConv1D,
        "quantized_bits": quantized_bits,
        "Clip": Clip,
        "Functional": Model,
    })

def sanitize_model_json(json_str: str) -> str:
    model_config = json.loads(json_str)

    def clean_config(layer):
        layer_config = layer.get("config", {})
        if layer["class_name"] == "QDepthwiseConv2D":
            for key in ["groups", "kernel_regularizer", "kernel_constraint"]:
                layer_config.pop(key, None)

        elif layer["class_name"] == "QSeparableConv2D":
            for key in [
                "groups",
                "kernel_initializer",  # <-- REMOVE THIS TOO
                "kernel_regularizer",
                "kernel_constraint"
            ]:
                layer_config.pop(key, None)

    for layer in model_config.get("config", {}).get("layers", []):
        clean_config(layer)

    return json.dumps(model_config)

class Model_Generator:
    failed_models = 0

    def __init__(self):
        self.reset_layers()

    def config_layer(self, layer_type: Layer) -> dict:
        """
        Returns hyper parameters for layer initialization as a dict

        arguments:
        layer_type -- takes in the selection of layer so it can specify
        """
        activation = random.choices(self.activations, weights=self.params['probs']['activations'], k=1)[0]
        use_bias = random.random() < self.params['bias_rate']

        if layer_type in self.dense_layers:
            layer_size = clip_base_2(random.randint(self.params['dense_lb'], self.params['dense_ub']))
            dropout = random.random() < self.params['dropout_chance']

            hyper_params = {'size': layer_size, 'activation': activation, 'use_bias': use_bias,
                            'dropout': dropout, 'dropout_rate': self.params['dropout_rate']}
        elif layer_type in self.conv_layers:
            out_filters = clip_base_2(random.randint(3, 256))
            flatten = (random.random() < self.params['flatten_chance']) or \
                      (self.params['last_layer_shape'][0] < self.params['conv_flatten_limit'] or
                       self.params['last_layer_shape'][1] < self.params['conv_flatten_limit'])
            
            pooling = random.random() < self.params['pooling_chance']
            padding = random.choices(['same', 'valid'], weights=self.params['probs']['padding'], k=1)[0]
            kernel_size = min(random.randint(self.params['conv_kernel_lb'], self.params['conv_kernel_ub']),
                              *self.params['last_layer_shape'][:-1])
            
            stride = random.randint(self.params['conv_stride_lb'], self.params['conv_stride_ub'])
            row_dim_pred = (self.params['last_layer_shape'][0] - kernel_size + 2 * int(padding == 'valid')) / stride + 1
            col_dim_pred = (self.params['last_layer_shape'][1] - kernel_size + 2 * int(padding == 'valid')) / stride + 1
            
            if row_dim_pred <= 0 or col_dim_pred <= 0:
                kernel_size, stride, padding = 1, 1, 'same'

            hyper_params = {'out_filters': out_filters, 'kernel': (kernel_size, kernel_size),
                            'flatten': flatten, 'activation': activation, 'use_bias': use_bias,
                            'pooling': pooling, 'padding': padding, 'stride': (stride, stride)}
        elif layer_type in self.time_layers:
            out_filters = clip_base_2(random.randint(3, 256))
            kernel_size = random.randint(self.params['conv_kernel_lb'], self.params['conv_kernel_ub'])
            flatten = random.random() < self.params['flatten_chance']
            stride = random.randint(self.params['conv_stride_lb'], self.params['conv_stride_ub'])
            padding = random.choices(['same', 'valid'], weights=self.params['probs']['padding'], k=1)[0]

            hyper_params = {'out_filters': out_filters, 'kernel': kernel_size,
                            'flatten': flatten, 'activation': activation, 'use_bias': use_bias,
                            'padding': padding, 'stride': stride}
        return hyper_params

    def next_layer(self, last_layer: Layer, input_layer: Layer = None, pre_config: dict = None) -> Layer:
        """
        Takes previous layer and configuration displays and returns back layer
        
        arguments:
        last_layer -- previous keras/qkeras layer
        """
                
        if 'dense' in self.name:
            layer_type = random.choices(self.dense_layers, weights=self.params['probs']['dense_layers'], k=1)[0] if input_layer is None else last_layer
            hyper_params = self.config_layer(layer_type) if not pre_config else pre_config
            
            last_layer = last_layer if input_layer is None else input_layer
            
            if self.q_on:
                layer_choice = [layer_type(hyper_params['size'],
                                           kernel_quantizer=quantized_bits(self.params['weight_bit_width'],
                                                                           self.params['weight_int_width']),
                                           use_bias=hyper_params['use_bias'])(last_layer)]
                
                if "no_activation" not in hyper_params['activation']:
                    layer_choice.append(QActivation(activation=hyper_params['activation'])(layer_choice[-1]))
            else:
                layer_choice = [layer_type(hyper_params['size'],
                                           use_bias=hyper_params['use_bias'])(last_layer)]
                if hyper_params['dropout']:
                    layer_choice.append(keras.layers.Dropout(hyper_params['dropout_rate'])(layer_choice[-1]))
                
                if "no_activation" not in hyper_params['activation']:
                    layer_choice.append(Activation(activation=hyper_params['activation'])(layer_choice[-1]))

            self.name = 'dense'
        elif 'conv' in self.name:
            layer_type = random.choices(self.conv_layers, weights=self.params['probs']['conv_layers'], k=1)[0] if input_layer is None else last_layer
            if input_layer is None:
                self.params['last_layer_shape']
            
            hyper_params = self.config_layer(layer_type)
            
            last_layer = last_layer if input_layer is None else input_layer

            if self.q_on:
                if layer_type == QConv2D:
                    layer_choice = [layer_type(hyper_params['out_filters'], hyper_params['kernel'], strides=hyper_params['stride'],
                                               kernel_quantizer=quantized_bits(self.params['weight_bit_width'],
                                                                               self.params['weight_int_width']),
                                               use_bias=hyper_params['use_bias'], padding=hyper_params['padding'])(last_layer)]
                elif layer_type == QSeparableConv2D:
                    layer_choice = [layer_type(hyper_params['out_filters'], hyper_params['kernel'], strides=hyper_params['stride'],
                                               use_bias=hyper_params['use_bias'], padding=hyper_params['padding'])(last_layer)]
                elif layer_type == QDepthwiseConv2D:
                    layer_choice = [layer_type(hyper_params['kernel'], strides=hyper_params['stride'],
                                               use_bias=hyper_params['use_bias'], padding=hyper_params['padding'])(last_layer)]
                
                if "no_activation" not in hyper_params['activation']:
                    layer_choice.append(QActivation(activation=hyper_params['activation'])(layer_choice[-1]))

                if hyper_params['pooling']:
                    layer_choice.append(QAveragePooling2D()(layer_choice[-1]))
            else:
                layer_choice = [layer_type(hyper_params['out_filters'], hyper_params['kernel'], strides=hyper_params['stride'],
                                           use_bias=hyper_params['use_bias'], padding=hyper_params['padding'])(last_layer)]
                
                if "no_activation" not in hyper_params['activation']:
                    layer_choice.append(Activation(activation=hyper_params['activation'])(layer_choice[-1]))
                
                if hyper_params['pooling']:
                    pooling = random.choices([keras.layers.MaxPooling2D, keras.layers.AveragePooling2D],
                                             weights=self.params['probs']['pooling'], k=1)[0]
                    layer_choice.append(pooling((2, 2))(layer_choice[-1]))

            self.name = 'conv'

            if hyper_params['flatten'] and input_layer is None:
                layer_choice.append(Flatten()(last_layer))
                self.name = 'dense'
        elif 'time' in self.name:
            layer_type = random.choices(self.time_layers, weights=self.params['probs']['time_layers'], k=1)[0] if input_layer is None else last_layer
            if input_layer is None:
                self.params['last_layer_shape']
            hyper_params = self.config_layer(layer_type)

            last_layer = last_layer if input_layer is None else input_layer
            
            if self.q_on:
                if layer_type == QConv1D:
                    layer_choice = [layer_type(filters=hyper_params['out_filters'], kernel_size=hyper_params['kernel'],
                                               strides=hyper_params['stride'],
                                               kernel_quantizer=quantized_bits(self.params['weight_bit_width'],
                                                                               self.params['weight_int_width']),
                                               use_bias=hyper_params['use_bias'], padding=hyper_params['padding'])(last_layer)]
                elif layer_type == QSeparableConv1D:
                    layer_choice = [layer_type(filters=hyper_params['out_filters'], kernel_size=hyper_params['kernel'],
                                               strides=hyper_params['stride'],
                                               use_bias=hyper_params['use_bias'], padding=hyper_params['padding'])(last_layer)]
                elif layer_type == QLSTM:
                    raise NotImplemented
                
                if "no_activation" not in hyper_params['activation']:
                    layer_choice.append(QActivation(activation=hyper_params['activation'])(layer_choice[-1]))
            else:
                if layer_type == LSTM:
                    raise NotImplemented
                elif layer_type == Conv1D:
                    layer_choice = [layer_type(filters=hyper_params['out_filters'], kernel_size=hyper_params['kernel'],
                                               strides=hyper_params['stride'],
                                               use_bias=hyper_params['use_bias'], padding=hyper_params['padding'])(last_layer)]
                    
                if "no_activation" not in hyper_params['activation']:
                    layer_choice.append(Activation(activation=hyper_params['activation'])(layer_choice[-1]))
            self.name = 'time'
            if hyper_params['flatten'] and input_layer is None:
                layer_choice.append(Flatten()(last_layer))
                self.name = 'dense'
        self.params['last_layer_shape'] = layer_choice[-1].shape[1:]
        self.layer_depth += 1
        return layer_choice

    def gen_network(self, total_layers: int = 3,
                    add_params: dict = {}, callback=None,
                    save_file: typing.IO = None) -> Model:
        
        """
        Generates interconnected network based on defaults or extra params, returns Model

        keyword arguments:
        total_layers -- total active layers in a network (default: 3)
        add_params -- parameters to specify besides defaults for model generation (default: {})
        q_chance -- the prob that we use qkeras over keras
        save_file -- open file descriptor for log file (default: None)
        """

        add_params = {k: add_params[k] for k in add_params}
        self.params = {
            'dense_lb': 32, 'dense_ub': 1024,
            'conv_init_size_lb': 32, 'conv_init_size_ub': 128,
            'conv_filters_lb': 3, 'conv_filters_ub': 64,
            'conv_stride_lb': 1, 'conv_stride_ub': 3,
            'conv_kernel_lb': 1, 'conv_kernel_ub': 6,
            'time_lb': 30, 'time_ub': 150,
            'conv_flatten_limit': 8,
            'q_chance': .5,
            'activ_bit_width': 8, 'activ_int_width': 4,
            'weight_bit_width': 6, 'weight_int_width': 3,
            'probs': {
                'activations': [],
                'dense_layers': [], 'conv_layers': [], 'start_layers': [], 'time_layers': [],
                'padding': [0.5, 0.5],
                'pooling': [0.5, 0.5]
            },
            'activation_rate': .5,
            'dropout_chance': .5,
            'dropout_rate': .4,
            'flatten_chance': .5,
            'pooling_chance': .5,
            'bias_rate': .5
            }
        
        self.params.update(add_params)
        # wipe either all the qkeras or keras layers depending on what mode we're in
        self.filter_q(self.params['q_chance'], self.params)
        
        init_layer = random.choices(self.start_layers, weights=self.params['probs']['start_layers'], k=1)[0]
        layer_units = 0

        # gen size based off start layer (right now is dense so can manipulate first selection)
        if init_layer in self.dense_layers:
            input_shape = (clip_base_2(random.randint(self.params['dense_lb'], self.params['dense_ub'])),)
        elif init_layer in self.conv_layers:
            y_dim = random.randint(self.params['conv_init_size_lb'], self.params['conv_init_size_ub'])
            x_dim = random.randint(self.params['conv_init_size_lb'], self.params['conv_init_size_ub'])
            num_filters = clip_base_2(random.randint(self.params['conv_filters_lb'], self.params['conv_filters_ub']))
            input_shape = (y_dim, x_dim, num_filters)
        elif init_layer in self.time_layers:
            input_shape = (clip_base_2(random.randint(self.params['time_lb'], self.params['time_ub'])),
                           random.randint(self.params['dense_lb'], self.params['dense_ub']))
        try:
            layers = [Input(shape=input_shape)]

            # create the initial layer to go off of
            self.params['last_layer_shape'] = layers[0].shape[1:]

            if init_layer in self.dense_layers:
                self.name = "dense"
            elif init_layer in self.conv_layers:
                self.name = "conv"
            elif init_layer in self.time_layers:
                self.name = "time"
            else:
                raise Exception("Layer not of a valid type")
            
            self.layer_depth += 1
            layers.extend(self.next_layer(init_layer, input_layer=layers[0]))
            while layer_units < total_layers:
                # provides a callback function. Will return if any value is instructed to return from the call
                if callback:
                    callback_output = callback(self, layers)
                    if callback_output:
                        return callback_output
                
                # disables dropout on last layer
                if layer_units == total_layers - 2 and self.name:
                    self.params['flatten_chance'] = 1
                if layer_units == total_layers - 1:
                    self.params['dropout_rate'] = 0

                layers.extend(self.next_layer(layers[-1]))
                layer_units += 1
        
            model = Model(inputs=layers[0], outputs=layers[-1])
        
            if save_file:
                json_models = json.dumps(model, indent=None)
                with open(save_file, "w") as file:
                    file.write(json_models)
                
            return model
        
        except ValueError as e:
            self.failed_models += 1
            self.reset_layers()
            return self.gen_network(total_layers=total_layers,
                                    add_params=add_params, callback=callback,
                                    save_file=save_file)

    def reset_layers(self) -> None:
        """
        Used to return class to initial state. Useful if generating multiple networks
        """
        self.dense_layers = [Dense, QDense]
        self.conv_layers = [QConv2D, Conv2D, QSeparableConv2D, QDepthwiseConv2D]
        self.time_layers = [Conv1D, QConv1D]
        self.start_layers = [Conv1D, QConv1D, Conv2D, QConv2D, QDense, Dense, QSeparableConv2D, QDepthwiseConv2D]

        self.activations = ["no_activation", "relu", "tanh", "sigmoid", "softmax"]
        
        self.layer_depth = 0

    def filter_q(self, q_chance: float, params: dict) -> None:
        blacklist = []
        self.q_on = random.random() < q_chance

        # filter out the qkeras/non-qkeras layers
        for layer in set(self.start_layers + self.conv_layers + self.dense_layers):
            is_qkeras = layer.__module__[:6] == 'qkeras'
            if self.q_on ^ is_qkeras:
                blacklist.append(layer)
        self.start_layers = [layer for layer in self.start_layers if layer not in blacklist]
        self.dense_layers = [layer for layer in self.dense_layers if layer not in blacklist]
        self.conv_layers = [layer for layer in self.conv_layers if layer not in blacklist]
        self.time_layers = [layer for layer in self.time_layers if layer not in blacklist]

        # adjust activation layers based on quantization
        if self.q_on:
            if 'softmax' in self.activations:
                self.activations.remove('softmax')
            self.activations = [f'quantized_{activ_func}({params["activ_bit_width"]},{params["activ_int_width"]})' for
                                activ_func in self.activations]
            
        # defaults if the layer was not set. Setting these is intentionally very delicate
        pairs = {'activations': self.activations, 'start_layers': self.start_layers, 'dense_layers': self.dense_layers,
                 'conv_layers': self.conv_layers, 'time_layers': self.time_layers}
        for param_type in pairs:
            if param_type not in self.params['probs']:
                self.params['probs'][param_type] = []

            if not self.params['probs'][param_type]:
                self.params['probs'][param_type] = [1 / len(pairs[param_type]) for _ in pairs[param_type]]

        for p_type in ['padding', 'pooling']:
            if p_type not in self.params['probs']:
                self.params['probs'][p_type] = [.5, .5]

    def load_models(self, save_file: str) -> list:
        """
        Parses and returns an iterable of generated models from a JSON lines file,
        where each line contains one or more models as stringified Keras JSON configs.
        Expects a comma separated list.

        Arguments:
        save_file -- path to JSONL file
        """

        with open(save_file, "r") as f:
            json_model_list = json.load(f)

        for model_label in json_model_list:
            model_json = json_model_list[model_label]
            
            co = {}
            _add_supported_quantized_objects(co)

            cleaned_json = sanitize_model_json(model_json)
            yield model_from_json(cleaned_json, custom_objects=co)

ray.init(num_cpus=os.cpu_count(), log_to_driver=False)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

@ray.remote(max_retries=10, retry_exceptions=True)
def generate_model(bitwidth, mg, params):
    try:
        model = mg.gen_network(add_params=params,
                            total_layers=random.randint(3, 10), save_file=None)
        return model.name, model.to_json()
    except Exception as e:
        mg.failed_models += 1
        raise(e)

def threaded_exec(batch_range: int, batch_size: int, params: dict = {}):
    succeeded = 0

    assert batch_range > 0
    assert batch_size > 0

    for batch_i in tqdm(range(batch_range), desc="Batch Count:"):
        models = []
        futures = [generate_model.remote(2 ** random.randint(2, 4), Model_Generator(), params) for _ in range(batch_size)]
        for future in ray.get(futures):
            model_name, model_json = future
            if model_name and model_json:
                models.append(model_json)
                succeeded +=1
        json_models = json.dumps(models, indent=None, separators=(',', '\n'))
        with open(f"conv2d_batch_{batch_i}.json", "w") as file:
            file.write(json_models)
        print(succeeded)
        print(len(models))

from collections import defaultdict
if __name__ == '__main__':
    # threaded_exec(batch_range, batch_size)
    
    # left this here as an example but everything in __name__ can be deleted
    def callback(mg: Model_Generator, layers: list):
        if mg.layer_depth > 1:
            mg.params['flatten_chance'] = 1

    mg = Model_Generator()
    model_data = {}
    for batch_file in os.listdir('conv2d_models'):
        for model in mg.load_models(f"conv2d_models/{batch_file}"):
            # get number of layers
            layer_cnt = 0
            cur_data = defaultdict(int)
            for layer in model.layers:
                if 'conv' in layer.name or 'dense' in layer.name:
                    if 'conv1d' in layer.name:
                        cur_data['conv1d'] += 1
                    if 'conv2d' in layer.name:
                        cur_data['conv2d'] += 1
                    if 'dense' in layer.name:
                        cur_data['dense'] += 1
                    layer_cnt += 1
                elif 'activation' in layer.name:
                    cur_data['activation'] += 1
                elif 'pool' in layer.name:
                    cur_data['pool'] += 1

            if layer_cnt not in model_data:
                model_data[layer_cnt] = defaultdict(int)
            model_data[layer_cnt]['model_cnt'] += 1
            model_data[layer_cnt]['conv'] += cur_data['conv2d'] + cur_data['conv1d']
            model_data[layer_cnt]['dense'] += cur_data['dense']
            model_data[layer_cnt]['conv1d'] += cur_data['conv1d']
            model_data[layer_cnt]['conv2d'] += cur_data['conv2d']
            model_data[layer_cnt]['activation'] += cur_data['activation']
            model_data[layer_cnt]['pooling'] += cur_data['pool']
            model_data[layer_cnt]['only_dense'] += int(cur_data['conv2d'] == 0 and cur_data['conv1d'] == 0)

    # saves the metadata
    serializable_model_data = {k: dict(v) for k, v in model_data.items()}
    with open("model_layer_metadata.json", "w") as f:
        json.dump(serializable_model_data, f, indent=2)
