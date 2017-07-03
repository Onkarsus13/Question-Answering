from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import time
import logging

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf
from general_utils import Progbar
from data_utils import *

from attention_wrapper_orig import *
#from tensorflow.contrib.seq2seq import BahdanauAttention, AttentionWrapper
from evaluate import exact_match_score, f1_score

logging.basicConfig(level=logging.INFO)


def get_optimizer(opt):
    if opt == "adam":
        optfn = tf.train.AdamOptimizer
    elif opt == "sgd":
        optfn = tf.train.GradientDescentOptimizer
    else:
        assert (False)
    return optfn


class Encoder(object):
    def __init__(self, hidden_size, initializer = tf.contrib.layers.xavier_initializer):
        self.hidden_size = hidden_size
        self.init_weights = initializer
        self.setup_params()


    def setup_params(self):
        with tf.variable_scope("encoder_lstm_question"):
            lstm_cell_question = tf.contrib.rnn.LSTMCell(self.hidden_size, initializer = self.init_weights(), state_is_tuple = True)
        with tf.variable_scope("encoder_lstm_passage"):
            lstm_cell_passage  = tf.contrib.rnn.LSTMCell(self.hidden_size, initializer = self.init_weights(), state_is_tuple = True)

        self.lstm_cell_question = lstm_cell_question
        self.lstm_cell_passage = lstm_cell_passage


    def encode(self, inputs, masks, encoder_state_input = None):
        """
        :param inputs: vector representations of question and passage (a tuple) 
        :param masks: masking sequences for both question and passage (a tuple)

        :param encoder_state_input: (Optional) pass this as initial hidden state
                                    to tf.nn.dynamic_rnn to build conditional representations
        :return: an encoded representation of the question and passage.
        """


        question, passage = inputs
        masks_question, masks_passage = masks    


        # read passage conditioned upon the question
        with tf.variable_scope("encoded_question"):
            encoded_question, _ = tf.nn.dynamic_rnn(self.lstm_cell_question, question, masks_question, dtype=tf.float32) # (-1, Q, H)

        with tf.variable_scope("encoded_passage"):
            encoded_passage, _ =  tf.nn.dynamic_rnn(self.lstm_cell_passage, passage, masks_passage, dtype=tf.float32) # (-1, P, H)


        return encoded_question, encoded_passage

   
class Decoder(object):
    def __init__(self, hidden_size, initializer=tf.contrib.layers.xavier_initializer):
        self.hidden_size = hidden_size
        self.init_weights = initializer
        self.setup_params()


    def setup_params(self):
        with tf.variable_scope("match_lstm_forward"):
            cell1 = tf.contrib.rnn.LSTMCell(self.hidden_size, initializer = self.init_weights(), state_is_tuple = True )
        with tf.variable_scope("match_lstm_backward"):
            cell2 = tf.contrib.rnn.LSTMCell(self.hidden_size, initializer = self.init_weights(), state_is_tuple = True )

        with tf.variable_scope("answer_ptr_cell"):
            cell_answer_ptr = tf.contrib.rnn.LSTMCell(self.hidden_size, initializer = self.init_weights(), state_is_tuple = True )

        self.cell1 = cell1
        self.cell2 = cell2
        self.cell_answer_ptr = cell_answer_ptr


    def run_match_lstm(self, encoded_rep, masks):
        encoded_question, encoded_passage = encoded_rep
        masks_question, masks_passage = masks

        match_lstm_cell_attention_fn = lambda curr_input, state : tf.concat([curr_input, state], axis = -1)
        query_depth = encoded_question.get_shape()[-1]

        with tf.variable_scope("match_lstm_attention_mechanism"):
            attention_mechanism_match_lstm = BahdanauAttention(query_depth, encoded_question, memory_sequence_length = masks_question)

    
        # output attention is false because we want to output the cell output and not the attention values

        forward_lstm_attender  =  AttentionWrapper(self.cell1, attention_mechanism_match_lstm, output_attention = False, attention_input_fn = match_lstm_cell_attention_fn)
        backward_lstm_attender = AttentionWrapper(self.cell2, attention_mechanism_match_lstm, output_attention = False, attention_input_fn = match_lstm_cell_attention_fn)


        with tf.variable_scope("match_lstm_attender"):
            (output_attender_fw, output_attender_bw) , _ = tf.nn.bidirectional_dynamic_rnn(forward_lstm_attender, backward_lstm_attender,encoded_passage, dtype=tf.float32)
        
        output_attender = tf.concat([output_attender_fw, output_attender_bw], axis = -1) # (-1, P, 2*H)
        return output_attender


    def run_answer_ptr(self, output_attender, masks):
        batch_size = tf.shape(output_attender)[0]
        dummy_input = tf.ones([batch_size, 2, 10])
        masks_question, masks_passage = masks

   

        answer_ptr_cell_input_fn = lambda curr_input, curr_attention : curr_attention # independent of question
        query_depth_answer_ptr = output_attender.get_shape()[-1]


        with tf.variable_scope("answer_ptr_attention_mechanism"):
            attention_mechanism_answer_ptr = BahdanauAttention(query_depth_answer_ptr , output_attender, memory_sequence_length = masks_passage)

        # output attention is true because we want to output the attention values
        answer_ptr_attender = AttentionWrapper(self.cell_answer_ptr, attention_mechanism_answer_ptr, cell_input_fn = answer_ptr_cell_input_fn)

        with tf.variable_scope("answer_ptr_attender"):
            logits, _ = tf.nn.dynamic_rnn(answer_ptr_attender, dummy_input, dtype = tf.float32)
        return logits


    def decode(self, encoded_rep, masks):
        """
        takes in encoded_rep
        and output a probability estimation over
        all paragraph tokens on which token should be
        the start of the answer span, and which should be
        the end of the answer span.

        :param encoded_rep: 
        :param masks


        :return: logits: for each word in passage the probability that it is the start word and end word.
        """

        output_attender = self.run_match_lstm(encoded_rep, masks)
        logits = self.run_answer_ptr(output_attender, masks)
    
        return logits
    




class QASystem(object):
    def __init__(self, encoder, decoder, pretrained_embeddings, config):
        """
        Initializes your System

        :param encoder: an encoder that you constructed in train.py
        :param decoder: a decoder that you constructed in train.py
        :param args: pass in more arguments as needed
        """

        # ==== set up placeholder tokens ========
        self.setup_placeholders()

        self.embeddings = pretrained_embeddings
        self.encoder = encoder
        self.decoder = decoder
        self.config = config


        # ==== assemble pieces ====
        with tf.variable_scope("qa"):
            self.setup_word_embeddings()
            self.setup_system()
            self.setup_loss()
            self.setup_train_op()

        # ==== set up training/updating procedure ====
        

    def setup_train_op(self):
        """
        Add train_op to self
        """
        with tf.variable_scope("train_step"):
            global_step = tf.contrib.framework.get_or_create_global_step()
            self.train_op = tf.contrib.layers.optimize_loss(
                loss=self.loss,
                global_step=global_step,
                learning_rate=self.config.lr,
                optimizer='Adam')

        self.init = tf.global_variables_initializer()


    def get_feed_dict(self, questions, contexts, answers):
        """
        :return: dict {placeholders: value}
        """

        padded_questions, question_lengths = pad_sequences(questions, 0)
        padded_contexts, passage_lengths = pad_sequences(contexts, 0)

        # print(padded_questions)
        # print(question_lengths)
        # print(passage_lengths)

        # print(answers)

        feed = {
            self.question_ids : padded_questions,
            self.passage_ids : padded_contexts,
            self.question_lengths : question_lengths,
            self.passage_lengths : passage_lengths,
            self.labels : answers
        }

        return feed


    def setup_word_embeddings(self):
        with tf.variable_scope("vocab_embeddings"):
            _word_embeddings = tf.Variable(self.embeddings, name="_word_embeddings", dtype=tf.float32, trainable= self.config.train_embeddings)
            self.question = tf.nn.embedding_lookup(_word_embeddings, self.question_ids, name = "question") # (-1, Q, D)
            self.passage = tf.nn.embedding_lookup(_word_embeddings, self.passage_ids, name = "passage") # (-1, P, D)



    def setup_placeholders(self):
        self.question_ids = tf.placeholder(tf.int32, shape = [None, None], name = "question_ids")
        self.passage_ids = tf.placeholder(tf.int32, shape = [None, None], name = "passage_ids")

        self.question_lengths = tf.placeholder(tf.int32, shape=[None], name="question_lengths")
        self.passage_lengths = tf.placeholder(tf.int32, shape = [None], name = "passage_lengths")

        self.labels = tf.placeholder(tf.int32, shape = [None, 2], name = "gold_labels")


    def setup_system(self):
        """
            DOCUMENTATION TODO
        """
        encoder = self.encoder
        decoder = self.decoder
        encoded_question, encoded_passage = encoder.encode([self.question, self.passage], [self.question_lengths, self.passage_lengths],
                                                             encoder_state_input = None)

        logits = decoder.decode([encoded_question, encoded_passage], [self.question_lengths, self.passage_lengths])

        self.logits = logits

        


    def setup_loss(self):
        """
        Set up your loss computation here
        :return:
        """

        losses = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=self.logits, labels=self.labels)
        self.loss = tf.reduce_mean(losses)


    def test(self, session, valid_x, valid_y):
        """
        in here you should compute a cost for your validation set
        and tune your hyperparameters according to the validation set performance
        :return:
        """
        input_feed = {}

        # fill in this feed_dictionary like:
        # input_feed['valid_x'] = valid_x

        output_feed = []

        outputs = session.run(output_feed, input_feed)

        return outputs

    def decode(self, session, test_x):
        """
        Returns the probability distribution over different positions in the paragraph
        so that other methods like self.answer() will be able to work properly
        :return:
        """
        input_feed = {}

        # fill in this feed_dictionary like:
        # input_feed['test_x'] = test_x

        output_feed = []

        outputs = session.run(output_feed, input_feed)

        return outputs

    def answer(self, session, test_x):

        yp, yp2 = self.decode(session, test_x)

        a_s = np.argmax(yp, axis=1)
        a_e = np.argmax(yp2, axis=1)

        return (a_s, a_e)

    def validate(self, sess, valid_dataset):
        """
        Iterate through the validation dataset and determine what
        the validation cost is.

        This method calls self.test() which explicitly calculates validation cost.

        How you implement this function is dependent on how you design
        your data iteration function

        :return:
        """
        valid_cost = 0

        for valid_x, valid_y in valid_dataset:
          valid_cost = self.test(sess, valid_x, valid_y)


        return valid_cost

    def evaluate_answer(self, session, dataset, sample=100, log=False):
        """
        Evaluate the model's performance using the harmonic mean of F1 and Exact Match (EM)
        with the set of true answer labels

        This step actually takes quite some time. So we can only sample 100 examples
        from either training or testing set.

        :param session: session should always be centrally managed in train.py
        :param dataset: a representation of our data, in some implementations, you can
                        pass in multiple components (arguments) of one dataset to this function
        :param sample: how many examples in dataset we look at
        :param log: whether we print to std out stream
        :return:
        """

        f1 = 0.
        em = 0.

        if log:
            logging.info("F1: {}, EM: {}, for {} samples".format(f1, em, sample))

        return f1, em


    def run_epoch(self, session, train, dev, epoch):
        """
        Perform one complete pass over the training data and evaluate on dev
        """

        nbatches = (len(train) + self.config.batch_size - 1) / self.config.batch_size
        prog = Progbar(target=nbatches)

        for i, (q_batch, c_batch, a_batch) in enumerate(minibatches(train, self.config.batch_size)):
            input_feed = self.get_feed_dict(q_batch, c_batch, a_batch)

            _, train_loss, logits = session.run([self.train_op, self.loss, self.logits], feed_dict=input_feed)

            print(logits)
            print("="*50)
            print(logits.shape)
            print("="*50)
            print(logits.sum(axis=-1))
            prog.update(i + 1, [("train loss", train_loss)])

 

    def train(self, session, dataset, train_dir):
        """
        Implement main training loop

        TIPS:
        You should also implement learning rate annealing (look into tf.train.exponential_decay)
        Considering the long time to train, you should save your model per epoch.

        More ambitious appoarch can include implement early stopping, or reload
        previous models if they have higher performance than the current one

        As suggested in the document, you should evaluate your training progress by
        printing out information every fixed number of iterations.

        We recommend you evaluate your model performance on F1 and EM instead of just
        looking at the cost.

        :param session: it should be passed in from train.py
        :param dataset: a representation of our data, in some implementations, you can
                        pass in multiple components (arguments) of one dataset to this function
        :param train_dir: path to the directory where you should save the model checkpoint
        :return:
        """

        # some free code to print out number of parameters in your model
        # it's always good to check!
        # you will also want to save your model parameters in train_dir
        # so that you can use your trained model to make predictions, or
        # even continue training

        train, dev = dataset

        tic = time.time()
        params = tf.trainable_variables()
        num_params = sum(map(lambda t: np.prod(tf.shape(t.value()).eval()), params))
        toc = time.time()
        print("Number of params: %d (retreival took %f secs)" % (num_params, toc - tic))


        for epoch in xrange(self.config.num_epochs):
            session.run(self.init)
            self.run_epoch(session, dev, dev, epoch)