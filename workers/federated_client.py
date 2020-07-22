#-----------------------------------------------------------------------------------------------#
#                                                                                               #
#   I M P O R T     G L O B A L     L I B R A R I E S                                           #
#                                                                                               #
#-----------------------------------------------------------------------------------------------#
import asyncio
import binascii
import logging
import ssl
import copy
from typing import Union
from typing import List

import tblib.pickling_support
import torch
import websockets

from torch.utils.data import BatchSampler, RandomSampler, SequentialSampler
import numpy as np

import syft as sy
from syft.generic.abstract.tensor import AbstractTensor
from syft.workers.virtual import VirtualWorker

from syft.exceptions import GetNotPermittedError
from syft.exceptions import ResponseSignatureError

tblib.pickling_support.install()

#***********************************************************************************************#
#                                                                                               #
#   description:                                                                                #
#   class that implements the logic for Federated Worker nodes.                                 #
#                                                                                               #
#***********************************************************************************************#
class FederatedWorker(VirtualWorker):
    def __init__(
        self,
        hook,
        host: str,
        port: int,
        id: Union[int, str] = 0,
        log_msgs: bool = False,
        verbose: bool = False,
        data: List[Union[torch.Tensor, AbstractTensor]] = None,
        loop=None,
        cert_path: str = None,
        key_path: str = None,
        datasets = None,
        models = None,
    ):
        """This is a simple extension to normal workers wherein
        all messages are passed over websockets. Note that because
        BaseWorker assumes a request/response paradigm, this worker
        enforces this paradigm by default.
        Args:
            hook (sy.TorchHook): a normal TorchHook object
            id (str or id): the unique id of the worker (string or int)
            log_msgs (bool): whether or not all messages should be
                saved locally for later inspection.
            verbose (bool): a verbose option - will print all messages
                sent/received to stdout
            host (str): the host on which the server should be run
            port (int): the port on which the server should be run
            data (dict): any initial tensors the server should be
                initialized with (such as datasets)
            loop: the asyncio event loop if you want to pass one in
                yourself
            cert_path: path to used secure certificate, only needed for secure connections
            key_path: path to secure key, only needed for secure connections
        """

        self.port = port
        self.host = host
        self.cert_path = cert_path
        self.key_path = key_path
        self.datasets = datasets if datasets is not None else dict()
        self.models = models if models is not None else dict()

        if loop is None:
            loop = asyncio.new_event_loop()

        # this queue is populated when messages are received
        # from a client
        self.broadcast_queue = asyncio.Queue()

        # this is the asyncio event loop
        self.loop = loop

        # call BaseWorker constructor
        super().__init__(hook=hook, id=id, data=data, log_msgs=log_msgs, verbose=verbose)

    def add_dataset(self, dataset, key: str):
        """Add new dataset to the current federated worker object.
        Args:
            dataset: a new dataset instance to be added.
            key: a unique identifier for the new dataset.
        """
        if key not in self.datasets:
            self.datasets[key] = dataset
        else:
            raise ValueError(f"Key {key} already exists in Datasets")
    
    def remove_dataset(self, key: str):
        """Remove a dataset from current federated worker object.
        Args:
            key: a unique identifier for the dataset to be removed
        """
        if key in self.datasets:
            del self.datasets[key]

    def add_model(self, model, key: str):
        """Add new model to the current federated worker object.
        Args:
            model: a new model instance to be added.
            key: a unique identifier for the new model.
        """
        if key not in self.models:
            self.models[key] = model
        else:
            raise ValueError(f"Key {key} already exists in Models")
    
    def remove_model(self, key: str):
        """Remove a model from current federated worker object.
        Args:
            key: a unique identifier for the model to be removed
        """
        if key in self.models:
            del self.models[key]

    async def _consumer_handler(self, websocket: websockets.WebSocketCommonProtocol):
        """This handler listens for messages from WebsocketClientWorker
        objects.
        Args:
            websocket: the connection object to receive messages from and
                add them into the queue.
        """
        try:
            while True:
                msg = await websocket.recv()
                await self.broadcast_queue.put(msg)
        except websockets.exceptions.ConnectionClosed:
            self._consumer_handler(websocket)

    async def _producer_handler(self, websocket: websockets.WebSocketCommonProtocol):
        """This handler listens to the queue and processes messages as they
        arrive.
        Args:
            websocket: the connection object we use to send responses
                back to the client.
        """
        while True:

            # get a message from the queue
            message = await self.broadcast_queue.get()

            # convert that string message to the binary it represent
            message = binascii.unhexlify(message[2:-1])

            # process the message
            response = self._recv_msg(message)

            # convert the binary to a string representation
            # (this is needed for the websocket library)
            response = str(binascii.hexlify(response))

            # send the response
            await websocket.send(response)

    def _recv_msg(self, message: bin) -> bin:
        try:
            return self.recv_msg(message)
        except (ResponseSignatureError, GetNotPermittedError) as e:
            return sy.serde.serialize(e)

    async def _handler(self, websocket: websockets.WebSocketCommonProtocol, *unused_args):
        """Setup the consumer and producer response handlers with asyncio.
        Args:
            websocket: the websocket connection to the client
        """

        asyncio.set_event_loop(self.loop)
        consumer_task = asyncio.ensure_future(self._consumer_handler(websocket))
        producer_task = asyncio.ensure_future(self._producer_handler(websocket))

        done, pending = await asyncio.wait([consumer_task, producer_task], return_when=asyncio.FIRST_COMPLETED)

        for task in pending:
            task.cancel()
    
    def set_train_config(self, **kwargs):
        """Set the training configuration in to the trainconfig object
        Args:
            **kwargs:
                add arguments here
        """
        self.lr = kwargs["lr"]
        self.plan_id = kwargs["plan_id"]
        self.model_id = kwargs["model_id"]
        self.model_param_id = kwargs["model_param_id"]
        self.batch_size = kwargs["batch_size"]
        self.max_nr_batches = kwargs["max_nr_batches"]
        return "SUCCESS"
    
    def fit(self, dataset_key: str, epoch: int, device: str = "cpu", **kwargs):
        """Fits a model on the local dataset as specified in the local TrainConfig object.
        Args:
            dataset_key: Identifier of the local dataset that shall be used for training.
            **kwargs: Unused.
        Returns:
            loss: Training loss on the last batch of training data.
        """
        if dataset_key not in self.datasets:
            raise ValueError(f"Dataset {dataset_key} unknown.")
        
        print("Fitting model on worker {0}".format(self.id))
        
        # get of build requirements
        train_plan = self.get_obj(self.plan_id)
        data_loader = self._create_data_loader(dataset_key=dataset_key, shuffle=False)
        
        # get the model parameters
        global_model = self.models[self.model_id]
        global_params = [param.data for param in global_model.parameters()]
        
        # get a local copy for training
        local_model = copy.deepcopy(global_model)
        local_params = [param.data for param in local_model.parameters()]
        
        # local variables for training
        losses = []
        accuracies = []        
        # starting training on all batches (need to modify this later to sample)
        for batch_idx, (input, targets) in enumerate(data_loader):
            input = input.view(self.batch_size, -1)
            y_hot = torch.nn.functional.one_hot(targets, 10)
            loss, acc, *local_params = train_plan.torchscript(
                input, y_hot, torch.tensor(self.batch_size), 
                torch.tensor(self.lr), local_params
            )
            losses.append(loss.item())
            accuracies.append(acc.item())
        
        # register losses array as a local object
        loss = torch.tensor(losses)
        loss.id = "loss"
        self.register_obj(loss)
        
        # compute change and send it back server
        difference = [a-b for a,b in zip(local_params,global_params)]
        
        # need to figure out a way to return this difference
        #print(difference)
        
        return None
    
    def _create_data_loader(self, dataset_key: str, shuffle: bool = False):
        """Helper function to create the dataloader as per our requirements
        """
        data_range = range(len(self.datasets[dataset_key]))

        # check requirements for data sampling
        if shuffle:
            sampler = RandomSampler(data_range)
        else:
            sampler = SequentialSampler(data_range)
        
        # create the dataloader as per our requirments
        data_loader = torch.utils.data.DataLoader(
            self.datasets[dataset_key],
            batch_size=self.batch_size,
            sampler=sampler,
            num_workers=0,
            drop_last=True,
        )
        return data_loader
    
    def start(self):
        """Start the websocket of the federated worker"""
        # Secure behavior: adds a secure layer applying cryptography and authentication
        if not (self.cert_path is None) and not (self.key_path is None):
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(self.cert_path, self.key_path)
            start_server = websockets.serve(
                self._handler,
                self.host,
                self.port,
                ssl=ssl_context,
                max_size=None,
                ping_timeout=None,
                close_timeout=None,
            )
        else:
            # Insecure
            start_server = websockets.serve(
                self._handler,
                self.host,
                self.port,
                max_size=None,
                ping_timeout=None,
                close_timeout=None,
            )

        asyncio.get_event_loop().run_until_complete(start_server)
        print("Serving. Press CTRL-C to stop.")
        try:
            asyncio.get_event_loop().run_forever()
        except KeyboardInterrupt:
            logging.info("Websocket server / federated worker stopped.")