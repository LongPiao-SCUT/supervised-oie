""" Usage:
    model --train=TRAIN_FN --test=TEST_FN [--glove=EMBEDDING]
"""
import numpy as np
import pandas
from docopt import docopt
from keras.models import Sequential, Model
from keras.layers import Input, Dense, LSTM, Embedding, TimeDistributedDense, TimeDistributed, merge, Bidirectional, Dropout
from keras.wrappers.scikit_learn import KerasClassifier
from keras.utils import np_utils
from keras.preprocessing.text import one_hot
from keras.preprocessing import sequence
from sklearn.model_selection import cross_val_score
from sklearn.model_selection import KFold
from sklearn.preprocessing import LabelEncoder
from sklearn.pipeline import Pipeline
from sklearn.metrics import accuracy_score
from load_pretrained_word_embeddings import Glove
from operator import itemgetter
from keras.callbacks import LambdaCallback
import logging
logging.basicConfig(level = logging.DEBUG)

class RNN_model:
    """
    Represents an RNN model for supervised OIE
    """
    def __init__(self,  model_fn, sent_maxlen, emb,
                 batch_size = 50, seed = 42, sep = '\t',
                 hidden_units = pow(2, 7),trainable_emb = True,
                 emb_dropout = 0.1, num_of_latent_layers = 2,
                 epochs = 10, pred_dropout = 0.1,
    ):
        """
        Initialize the model
        model_fn - a model generating function, to be called when training with self as a single argument.
        sent_maxlen - the maximum length in words of each sentence - will be used for padding / truncating
        batch_size - batch size for training
        pre_trained_emb - an embedding class
        seed - the random seed for reproduciblity
        sep  - separator in the csv dataset files for this model
        hidden_units - number of hidden units per layer
        trainable_emb - controls if the loss should propagate to the word embeddings during training
        emb_dropout - the percentage of dropout during embedding
        num_of_latent_layers - how many LSTMs to stack
        epochs - the number of epochs to train the model
        pred_dropout - the proportion to dropout before prediction
        """
        self.model_fn = lambda : model_fn(self)
        self.sent_maxlen = sent_maxlen
        self.batch_size = batch_size
        self.seed = seed
        self.sep = sep
        np.random.seed(self.seed)
        self.encoder = LabelEncoder()
        self.hidden_units = hidden_units
        self.emb = emb
        self.embedding_size = self.emb.dim
        self.trainable_emb = trainable_emb
        self.emb_dropout = emb_dropout
        self.num_of_latent_layers = num_of_latent_layers
        self.epochs = epochs
        self.pred_dropout = pred_dropout


    def plot(self, fn, train_fn):
        """
        Plot this model to an image file
        Train file is needed as it influences the dimentions of the RNN
        """
        from keras.utils.visualize_util import plot
        X, Y = self.load_dataset(train_fn)
        self.model_fn()
        plot(self.model, to_file = fn)


    def classes_(self):
        """
        Return the classes which are classified by this model
        """
        return self.encoder.classes_

    def train_and_test(self, train_fn, test_fn):
        """
        Train and then test on given files
        """
        self.train(train_fn)
        return self.test(test_fn)

    def train(self, train_fn):
        """
        Train this model on a given train dataset
        """
        X, Y = self.load_dataset(train_fn)
        logging.debug("Classes: {}".format((self.num_of_classes(), self.classes_())))
        # Set model params, called here after labels have been identified in load dataset
        self.model_fn()

        # Create a callback to print a sample after each epoch
        sample_output_callback = LambdaCallback(on_epoch_end =
                                                lambda epoch, logs:\
                                                pprint(self.sample_labels(self.model.predict(X)
                                                )))
        logging.debug("Training model on {}".format(train_fn))
        self.model.fit(X, Y,
                       batch_size = self.batch_size,
                       nb_epoch = self.epochs,
                       callbacks = [sample_output_callback])

    def test(self, test_fn):
        """
        Evaluate this model on a test file
        """
        X, Y = self.load_dataset(test_fn)
        self.predicted = np_utils.to_categorical(self.model.predict(X))
        acc = accuracy_score(Y, self.predicted) * 100
        logging.info("ACC: {:.2f}".format(acc))
        return acc

    def predict(self, input_fn):
        """
        Run this model on an input CoNLLL file
        Returns (gold, predicted)
        """
        X, Y = self.load_dataset(input_fn)
        return Y, self.model.predict(X)

    def load_dataset(self, fn):
        """
        Load a supervised OIE dataset from file
        Assumes that the labels appear in the last column.
        """
        df = pandas.read_csv(fn, sep = self.sep, header = 0)

        # Encode one-hot representation of the labels
        self.encoder.fit(df.label.values)

        # Split according to sentences and encode
        sents = self.get_sents_from_df(df)
        return (self.encode_inputs(sents),
                self.encode_outputs(sents))

    def get_sents_from_df(self, df):
        """
        Split a data frame by rows accroding to the sentences
        """
        return [df[df.run_id == i] for i in range(min(df.run_id), max(df.run_id))]

    def encode_inputs(self, sents):
        """
        Given a dataframe split to sentences, encode inputs for rnn classification.
        Should return a dictionary of sequences of sample of length maxlen.
        """
        # Encode inputs
        word_inputs = []
        pred_inputs = []
        for sent in sents:
            word_encodings = [self.emb.get_word_index(w) for w in sent.word.values]
            pred_word_encodings = [self.emb.get_word_index(w) for w in sent.pred.values]
            word_inputs.append([Sample(w) for w in word_encodings])
            pred_inputs.append([Sample(w) for w in pred_word_encodings])

        # Pad / truncate to desired maximum length
        ret = {"word_inputs" : [],
               "predicate_inputs": []}

        for name, sequence in zip(["word_inputs", "predicate_inputs"],
                                  [word_inputs, pred_inputs]):
            for samples in pad_sequences(sequence,
                                         pad_func = lambda : Pad_sample(),
                                         maxlen = self.sent_maxlen):
                ret[name].append([sample.encode() for sample in samples])

        return {k: np.array(v) for k, v in ret.iteritems()}


    def encode_outputs(self, sents):
        """
        Given a dataframe split to sentences, encode outputs for rnn classification.
        Should return a list sequence of sample of length maxlen.
        """
        output_encodings = []
        # Encode outputs
        for sent in sents:
            output_encodings.append(np_utils.to_categorical(self.encoder.transform(sent.label.values)))

        # Pad / truncate to maximum length
        return np.array(pad_sequences(output_encodings,
                                      lambda : np.array([0] * self.num_of_classes()),
                                      maxlen = self.sent_maxlen))


    def decode_label(self, encoded_label):
        """
        Decode a categorical representation of a label back to textual chunking label
        """
        return self.encoder.inverse_transform(encoded_label)

    def num_of_classes(self):
        """
        Return the number of ouput classes
        """
        return len(self.classes_())

    # Functional Keras -- all of the following are currying functions expecting models as input
    # https://keras.io/getting-started/functional-api-guide/

    def embed(self):
        """
        Embed word sequences using self's embedding class
        """
        return self.emb.get_keras_embedding(dropout = self.emb_dropout,
                                            trainable = self.trainable_emb,
                                            input_length = self.sent_maxlen)

    def predict_classes(self):
        """
        Predict to the number of classes
        Named arguments are passed to the keras function
        """

        return lambda x: self.stack(x,
                                    [lambda : TimeDistributed(Dense(output_dim = self.num_of_classes(),
                                                                    activation = "softmax"))] +
                                    [lambda : TimeDistributed(Dense(1028, activation='relu'))] * 3)

    def stack_latent_layers(self, n):
        """
        Stack n bidi LSTMs
        """
        return lambda x: self.stack(x, [lambda : Bidirectional(LSTM(self.hidden_units,
                                                                    return_sequences = True))] * n )

    def stack(self, x, layers):
        """
        Stack layers (FIFO) by applying recursively on the output, until returing the input
        as the base case for the recursion
        """
        if not layers:
            return x # Base case of the recursion is the just returning the input
        else:
            return layers[0]()(self.stack(x, layers[1:]))


            # return Bidirectional(LSTM(self.hidden_units,
            #                           return_sequences = return_sequences))\


    def set_vanilla_model(self):
        """
        Set a Keras sequential model for predicting OIE as a member of this class
        Can be passed as model_fn to the constructor
        """
        logging.debug("Setting vanilla model")
        # Build model

        ## Embedding Layer
        embedding_layer = self.embed()

        ## Deep layers
        latent_layers = self.stack_latent_layers(self.num_of_latent_layers)

        # ## Dropout
        dropout = Dropout(self.pred_dropout)

        ## Prediction
        predict_layer = self.predict_classes()

        ## Prepare input features, and indicate how to embed them
        inputs_and_embeddings = [(Input(shape = (self.sent_maxlen,),
                                       dtype="int32",
                                       name = "word_inputs"),
                                  embedding_layer),
                                 (Input(shape = (self.sent_maxlen,),
                                       dtype="int32",
                                        name = "predicate_inputs"),
                                  embedding_layer)]

        ## Concat all inputs and run on deep network
        output = predict_layer(dropout(latent_layers(merge([embed(inp) for inp, embed in inputs_and_embeddings],
                                                           mode = "concat",
                                                           concat_axis = -1))))

        # Build model
        self.model = Model(input = map(itemgetter(0), inputs_and_embeddings),
                           output = [output])

        # Loss
        self.model.compile(optimizer='adam',
                           loss='categorical_crossentropy',
                           metrics=['accuracy'])
        self.model.summary()

    def sample_labels(self, y, num_of_sents = 5, num_of_samples = 10, num_of_classes = 3, start_index = 5):
        """
        Get a sense of how labels in y look like
        """
        classes = self.classes_()
        ret = []
        for sent in y[:num_of_sents]:
            cur = []
            for word in sent[start_index: start_index + num_of_samples]:
                sorted_prob = am(word)
                cur.append([(classes[ind], word[ind]) for ind in sorted_prob[:num_of_classes]])
            ret.append(cur)
        return ret


class Sample:
    """
    Single sample representation.
    Containter which names spans in the input vector to simplify access
    """
    def __init__(self, word):
        self.word = word

    def encode(self):
        """
        Encode this sample as vector as input for rnn,
        Probably just concatenating members in the right order.
        """
        return self.word

class Pad_sample(Sample):
    """
    A dummy sample used for padding
    """
    def __init__(self):
        Sample.__init__(self, word = 0)

def pad_sequences(sequences, pad_func, maxlen = None):
    """
    Similar to keras.preprocessing.sequence.pad_sequence but using Sample as higher level
    abstraction.
    pad_func is a pad class generator.
    """
    ret = []

    # Determine the maxlen -- Make sure it doesn't exceed the maximum observed length
    max_value = max(map(len, sequences))
    if maxlen is None:
        maxlen = max_value
        logging.debug("Padding to maximum observed length ({})".format(max_value))
    else:
        maxlen = min(max_value, maxlen)
        logging.debug("Padding / truncating to {} words (max observed was {})".format(maxlen, max_value))

        # Pad / truncate (done this way to deal with np.array)
    for sequence in sequences:
        cur_seq = list(sequence[:maxlen])
        cur_seq.extend([pad_func()] * (maxlen - len(sequence)))
        ret.append(cur_seq)
    return ret


# Helper functions

## Argmaxes
am = lambda myList: [i[0] for i in sorted(enumerate(myList), key=lambda x:x[1], reverse= True)]

if __name__ == "__main__":
    from pprint import pprint
    args = docopt(__doc__)
    train_fn = args["--train"]
    test_fn = args["--test"]



    if "--glove" in args:
        emb = Glove(args["--glove"])
        rnn = RNN_model(model_fn = RNN_model.set_vanilla_model,
                        sent_maxlen = 20,
                        num_of_latent_layers = 3,
                        emb = emb,
                        epochs = 100)
        rnn.train(train_fn)
#        rnn.plot("./model.png", train_fn)
    Y, y1 = rnn.predict(train_fn)
    pprint(rnn.sample_labels(y1))
