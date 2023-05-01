# -*- coding: utf-8 -*-
"""Pareto/NBD model."""

from __future__ import print_function
from __future__ import division
import sqlalchemy       
import logging
from sqlalchemy.orm import load_only, relationship
from lifetimes import BetaGeoFitter
import pandas as pd
from sqlalchemy import func
from sqlalchemy import create_engine
from datetime import datetime, timedelta
from sqlalchemy.orm import joinedload, sessionmaker
from zenq.api.tables import Base, Facts 
import pandas as pd
import numpy as np
from numpy import log, exp, logaddexp, asarray, any as npany
from pandas import DataFrame
from scipy.special import gammaln, hyp2f1, betaln
from scipy.special import logsumexp
from scipy.optimize import minimize

from lifetimes.fitters import BaseFitter
from lifetimes.utils import _check_inputs, _scale_time
from lifetimes.generate_data import pareto_nbd_model


class ParetoNBDFitter(BaseFitter):
    """
    Pareto NBD fitter [7]_.

    Parameters
    ----------
    penalizer_coef: float
        The coefficient applied to an l2 norm on the parameters

    Attributes
    ----------
    penalizer_coef: float
        The coefficient applied to an l2 norm on the parameters
    params_: :obj: OrderedDict
        The fitted parameters of the model
    data: :obj: DataFrame
        A DataFrame with the columns given in the call to `fit`

    References
    ----------
    .. [7] David C. Schmittlein, Donald G. Morrison and Richard Colombo
       Management Science,Vol. 33, No. 1 (Jan., 1987), pp. 1-24
      "Counting Your Customers: Who Are They and What Will They Do Next,"
    """

    def __init__(
        self, db_uri,
        penalizer_coef=0.0
    ):

        self.engine = create_engine(db_uri)
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)()
        """
        Initialization, set penalizer_coef.
        """

        self.penalizer_coef = penalizer_coef
        
    def count_cltv(self):
        cltv_df = self.session.query(Facts.customer_id,
                                    func.DATE_TRUNC('day', func.max(Facts.date)) - func.DATE_TRUNC('day', func.min(Facts.date)),
                                    func.DATE_TRUNC('day', datetime.today()) - func.DATE_TRUNC('day', func.min(Facts.date)),
                                    func.count(Facts.invoice_id),
                                    func.sum(Facts.total_price)).\
                                    group_by(Facts.customer_id).\
                                    having(func.count(Facts.invoice_id) > 1).\
                                    all()
        cltv_df = pd.DataFrame(cltv_df, columns=['customer_id','recency', 'T', 'frequency', 'monetary'])
        cltv_df = cltv_df[cltv_df["monetary"] > 0]
        cltv_df = cltv_df[cltv_df["frequency"] > 0]
        cltv_df['recency'] = cltv_df['recency'].astype('timedelta64[D]').astype(float).map('{:.0f}'.format).astype(int)       
        cltv_df = cltv_df[cltv_df["recency"] > 0]

       
       
        rfm = pd.DataFrame()
        rfm["recency_score"] = pd.qcut(cltv_df['recency'], 5, labels=[5, 4, 3, 2, 1])
        rfm["frequency_score"] = pd.qcut(cltv_df["frequency"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5])
        rfm["monetary_score"] = pd.qcut(cltv_df["monetary"], 5, labels=[1, 2, 3, 4, 5])
        rfm["RFM_SCORE"] = (rfm["recency_score"].astype(str) + rfm["frequency_score"].astype(str))
        seg_map = {
            r'[1-2][1-2]': 'HIBERNATING',
            r'[1-2][3-4]': 'AT RISK',
            r'[1-2]5': 'CANT LOSE',
            r'3[1-2]': 'ABOUT TO SLEEP',
            r'33': 'NEED ATTENTION',
            r'[3-4][4-5]': 'LOYAL CUSTOMER',
            r'41': 'PROMISING',
            r'51': 'NEW CUSTOMERS',
            r'[4-5][2-3]': 'POTENTIAL LOYALIST',
            r'5[4-5]': 'CHAMPIONS'
        }
        rfm['segment'] = rfm['RFM_SCORE'].replace(seg_map, regex=True)
        return cltv_df, rfm

    def fit(
        self,
        frequency,
        recency,
        T,
        weights=None,
        iterative_fitting=1,
        initial_params=None,
        verbose=False,
        tol=1e-4,
        index=None,
        fit_method="Nelder-Mead",
        maxiter=2000,
        **kwargs
    ):
        """
        Pareto/NBD model fitter.

        Parameters
        ----------
        frequency: array_like
            the frequency vector of customers' purchases
            (denoted x in literature).
        recency: array_like
            the recency vector of customers' purchases
            (denoted t_x in literature).
        T: array_like
            customers' age (time units since first purchase)
        weights: None or array_like
            Number of customers with given frequency/recency/T,
            defaults to 1 if not specified. Fader and
            Hardie condense the individual RFM matrix into all
            observed combinations of frequency/recency/T. This
            parameter represents the count of customers with a given
            purchase pattern. Instead of calculating individual
            log-likelihood, the log-likelihood is calculated for each
            pattern and multiplied by the number of customers with
            that pattern.
        iterative_fitting: int, optional
            perform iterative_fitting fits over random/warm-started initial params
        initial_params: array_like, optional
            set the initial parameters for the fitter.
        verbose : bool, optional
            set to true to print out convergence diagnostics.
        tol : float, optional
            tolerance for termination of the function minimization process.
        index: array_like, optional
            index for resulted DataFrame which is accessible via self.data
        fit_method : string, optional
            fit_method to passing to scipy.optimize.minimize
        maxiter : int, optional
            max iterations for optimizer in scipy.optimize.minimize will be
            overwritten if set in kwargs.
        kwargs:
            key word arguments to pass to the scipy.optimize.minimize
            function as options dict

        Returns
        -------
        ParetoNBDFitter
            with additional properties like ``params_`` and methods like ``predict``
        """

        frequency = asarray(frequency).astype(int)
        recency = asarray(recency)
        T = asarray(T)

        if weights is None:
            weights = np.ones(recency.shape[0], dtype=np.int64)
        else:
            weights = asarray(weights)

        _check_inputs(frequency, recency, T)

        self._scale = _scale_time(T)
        scaled_recency = recency * self._scale
        scaled_T = T * self._scale

        params, self._negative_log_likelihood_ = self._fit(
            (frequency, scaled_recency, scaled_T, weights, self.penalizer_coef),
            iterative_fitting,
            initial_params,
            4,
            verbose,
            tol,
            fit_method,
            maxiter,
            **kwargs
        )
        self._hessian_ = None
        self.params_ = pd.Series(*(params, ["r", "alpha", "s", "beta"]))
        self.params_["alpha"] /= self._scale
        self.params_["beta"] /= self._scale

        self.data = DataFrame({"frequency": frequency, "recency": recency, "T": T, "weights": weights}, index=index)
        self.generate_new_data = lambda size=1: pareto_nbd_model(
            T, *self._unload_params("r", "alpha", "s", "beta"), size=size
        )

        self.predict = self.conditional_expected_number_of_purchases_up_to_time

        return self

    @staticmethod
    def _log_A_0(
        params, 
        freq, 
        recency, 
        age
    ):
        """
        log_A_0.
        
        Equation (19) and (20) from paper:
        http://brucehardie.com/notes/009/pareto_nbd_derivations_2005-11-05.pdf
        """

        r, alpha, s, beta = params

        if alpha < beta:
            min_of_alpha_beta, max_of_alpha_beta, t = (alpha, beta, r + freq)
        else:
            min_of_alpha_beta, max_of_alpha_beta, t = (beta, alpha, s + 1)
        abs_alpha_beta = max_of_alpha_beta - min_of_alpha_beta

        rsf = r + s + freq
        p_1 = hyp2f1(rsf, t, rsf + 1.0, abs_alpha_beta / (max_of_alpha_beta + recency))
        q_1 = max_of_alpha_beta + recency
        p_2 = hyp2f1(rsf, t, rsf + 1.0, abs_alpha_beta / (max_of_alpha_beta + age))
        q_2 = max_of_alpha_beta + age

        try:
            size = len(freq)
            sign = np.ones(size)
        except TypeError:
            sign = 1

        return logsumexp([log(p_1) + rsf * log(q_2), log(p_2) + rsf * log(q_1)], axis=0, b=[sign, -sign]) - rsf * log(
            q_1 * q_2
        )

    @staticmethod
    def _conditional_log_likelihood(
        params, 
        freq, 
        rec, 
        T
    ):
        """
        Implements equation (18) from:
        http://brucehardie.com/notes/009/pareto_nbd_derivations_2005-11-05.pdf
        """

        r, alpha, s, beta = params
        x = freq

        r_s_x = r + s + x

        A_1 = gammaln(r + x) - gammaln(r) + r * log(alpha) + s * log(beta)
        log_A_0 = ParetoNBDFitter._log_A_0(params, x, rec, T)

        A_2 = logaddexp(-(r + x) * log(alpha + T) - s * log(beta + T), log(s) + log_A_0 - log(r_s_x))

        return A_1 + A_2

    @staticmethod
    def _negative_log_likelihood(
        params, 
        freq, 
        rec, 
        T, 
        weights, 
        penalizer_coef
    ):
        """
        Sums the conditional log-likelihood from the ``_conditional_log_likelihood`` function
        and applies a ``penalizer_coef``.
        """

        if npany(asarray(params) <= 0.0):
            return np.inf

        conditional_log_likelihood = ParetoNBDFitter._conditional_log_likelihood(params, freq, rec, T)
        penalizer_term = penalizer_coef * sum(np.asarray(params) ** 2)

        return -(weights * conditional_log_likelihood).sum() / weights.mean() + penalizer_term

    def conditional_expected_number_of_purchases_up_to_time(
        self, 
        t, 
        frequency, 
        recency, 
        T
    ):
        """
        Conditional expected number of purchases up to time.

        Calculate the expected number of repeat purchases up to time t for a
        randomly choose individual from the population, given they have
        purchase history (frequency, recency, T).

        This is equation (41) from:
        http://brucehardie.com/notes/009/pareto_nbd_derivations_2005-11-05.pdf

        Parameters
        ----------
        t: array_like
            times to calculate the expectation for.
        frequency: array_like
            historical frequency of customer.
        recency: array_like
            historical recency of customer.
        T: array_like
            age of the customer.

        Returns
        -------
        array_like
        """

        x, t_x = frequency, recency
        params = self._unload_params("r", "alpha", "s", "beta")
        r, alpha, s, beta = params

        likelihood = self._conditional_log_likelihood(params, x, t_x, T)
        first_term = (
            gammaln(r + x) - gammaln(r) + r * log(alpha) + s * log(beta) - (r + x) * log(alpha + T) - s * log(beta + T)
        )
        second_term = log(r + x) + log(beta + T) - log(alpha + T)
        third_term = log((1 - ((beta + T) / (beta + T + t)) ** (s - 1)) / (s - 1))

        return exp(first_term + second_term + third_term - likelihood)

    def conditional_probability_alive(
        self, 
        frequency, 
        recency, 
        T
    ):
        """
        Conditional probability alive.

        Compute the probability that a customer with history
        (frequency, recency, T) is currently alive.

        Section 5.1 from (equations (36) and (37)):
        http://brucehardie.com/notes/009/pareto_nbd_derivations_2005-11-05.pdf

        Parameters
        ----------
        frequency: float
            historical frequency of customer.
        recency: float
            historical recency of customer.
        T: float
            age of the customer.

        Returns
        -------
        float
            value representing a probability

        """

        x, t_x = frequency, recency
        r, alpha, s, beta = self._unload_params("r", "alpha", "s", "beta")
        A_0 = self._log_A_0([r, alpha, s, beta], x, t_x, T)

        return 1.0 / (1.0 + exp(log(s) - log(r + s + x) + (r + x) * log(alpha + T) + s * log(beta + T) + A_0))

    def conditional_probability_alive_matrix(
        self, 
        max_frequency=None, 
        max_recency=None
    ):
        """
        Compute the probability alive matrix. 
        
        Builds on the ``conditional_probability_alive()`` method.

        Parameters
        ----------
        max_frequency: float, optional
            the maximum frequency to plot. Default is max observed frequency.
        max_recency: float, optional
            the maximum recency to plot. This also determines the age of the
            customer. Default to max observed age.

        Returns
        -------
        matrix:
            A matrix of the form [t_x: historical recency, x: historical frequency]
        """

        max_frequency = max_frequency or int(self.data["frequency"].max())
        max_recency = max_recency or int(self.data["T"].max())

        Z = np.zeros((max_recency + 1, max_frequency + 1))
        for i, recency in enumerate(np.arange(max_recency + 1)):
            for j, frequency in enumerate(np.arange(max_frequency + 1)):
                Z[i, j] = self.conditional_probability_alive(frequency, recency, max_recency)

        return Z

    def expected_number_of_purchases_up_to_time(
        self, 
        t
    ):
        """
        Return expected number of repeat purchases up to time t.

        Calculate the expected number of repeat purchases up to time t for a
        randomly choose individual from the population.

        Equation (27) from:
        http://brucehardie.com/notes/009/pareto_nbd_derivations_2005-11-05.pdf

        Parameters
        ----------
        t: array_like
            times to calculate the expectation for.

        Returns
        -------
        array_like
        """

        r, alpha, s, beta = self._unload_params("r", "alpha", "s", "beta")
        first_term = r * beta / alpha / (s - 1)
        second_term = 1 - (beta / (beta + t)) ** (s - 1)

        return first_term * second_term

    def conditional_probability_of_n_purchases_up_to_time(
        self, 
        n, 
        t, 
        frequency, 
        recency, 
        T
    ):
        """
        Return conditional probability of n purchases up to time t.

        Calculate the probability of n purchases up to time t for an individual
        with history frequency, recency and T (age).

        The main equation being implemented is (16) from:
        http://www.brucehardie.com/notes/028/pareto_nbd_conditional_pmf.pdf

        Parameters
        ----------
        n: int
            number of purchases.
        t: a scalar
            time up to which probability should be calculated.
        frequency: float
            historical frequency of customer.
        recency: float
            historical recency of customer.
        T: float
            age of the customer.

        Returns
        -------
        array_like
        """

        if t <= 0:
            return 0

        x, t_x = frequency, recency
        params = self._unload_params("r", "alpha", "s", "beta")
        r, alpha, s, beta = params

        if alpha < beta:
            min_of_alpha_beta, max_of_alpha_beta, p, _, _ = (alpha, beta, r + x + n, r + x, r + x + 1)
        else:
            min_of_alpha_beta, max_of_alpha_beta, p, _, _ = (beta, alpha, s + 1, s + 1, s)
        abs_alpha_beta = max_of_alpha_beta - min_of_alpha_beta

        log_l = self._conditional_log_likelihood(params, x, t_x, T)
        log_p_zero = (
            gammaln(r + x)
            + r * log(alpha)
            + s * log(beta)
            - (gammaln(r) + (r + x) * log(alpha + T) + s * log(beta + T) + log_l)
        )
        log_B_one = (
            gammaln(r + x + n)
            + r * log(alpha)
            + s * log(beta)
            - (gammaln(r) + (r + x + n) * log(alpha + T + t) + s * log(beta + T + t))
        )
        log_B_two = (
            r * log(alpha)
            + s * log(beta)
            + gammaln(r + s + x)
            + betaln(r + x + n, s + 1)
            + log(hyp2f1(r + s + x, p, r + s + x + n + 1, abs_alpha_beta / (max_of_alpha_beta + T)))
            - (gammaln(r) + gammaln(s) + (r + s + x) * log(max_of_alpha_beta + T))
        )

        def _log_B_three(i):
            return (
                r * log(alpha)
                + s * log(beta)
                + gammaln(r + s + x + i)
                + betaln(r + x + n, s + 1)
                + log(hyp2f1(r + s + x + i, p, r + s + x + n + 1, abs_alpha_beta / (max_of_alpha_beta + T + t)))
                - (gammaln(r) + gammaln(s) + (r + s + x + i) * log(max_of_alpha_beta + T + t))
            )

        zeroth_term = (n == 0) * (1 - exp(log_p_zero))
        first_term = n * log(t) - gammaln(n + 1) + log_B_one - log_l
        second_term = log_B_two - log_l
        third_term = logsumexp([i * log(t) - gammaln(i + 1) + _log_B_three(i) - log_l for i in range(n + 1)], axis=0)

        try:
            size = len(x)
            sign = np.ones(size)
        except TypeError:
            sign = 1

        # In some scenarios (e.g. large n) tiny numerical errors in the calculation of second_term and third_term
        # cause sumexp to be ever so slightly negative and logsumexp throws an error. Hence we ignore the sign here.
        return zeroth_term + exp(
            logsumexp([first_term, second_term, third_term], b=[sign, sign, -sign], axis=0, return_sign=True)[0]
        )

    def _fit(
        self,
        minimizing_function_args,
        iterative_fitting,
        initial_params,
        params_size,
        disp,
        tol=1e-6,
        fit_method="Nelder-Mead",
        maxiter=2000,
        **kwargs
    ):
        """
        Fit function for fitters.
        
        Minimizer Callback for this fitters class.
        """

        ll = []
        sols = []

        if iterative_fitting <= 0:
            raise ValueError("iterative_fitting parameter should be greater than 0 as of lifetimes v0.2.1")

        if iterative_fitting > 1 and initial_params is not None:
            raise ValueError(
                "iterative_fitting and initial_params should not be both set, as no improvement could be made."
            )

        # set options for minimize, if specified in kwargs will be overwritten
        minimize_options = {}
        minimize_options["disp"] = disp
        minimize_options["maxiter"] = maxiter
        minimize_options.update(kwargs)

        total_count = 0
        while total_count < iterative_fitting:
            current_init_params = (
                np.random.normal(1.0, scale=0.05, size=params_size) if initial_params is None else initial_params
            )
            if minimize_options["disp"]:
                print("Optimize function with {}".format(fit_method))

            output = minimize(
                self._negative_log_likelihood,
                method=fit_method,
                tol=tol,
                x0=current_init_params,
                args=minimizing_function_args,
                options=minimize_options,
            )
            sols.append(output.x)
            ll.append(output.fun)

            total_count += 1
        argmin_ll, min_ll = min(enumerate(ll), key=lambda x: x[1])
        minimizing_params = sols[argmin_ll]

        return minimizing_params, min_ll
    
    
    
import sqlalchemy
import pandas as pd
import IPython
from sqlalchemy.exc import IntegrityError
import ipywidgets
from sqlalchemy import Sequence, UniqueConstraint, create_engine, desc, asc
from datetime import datetime
from sqlalchemy import create_engine
from .tables import  Facts 
from sqlalchemy.orm import sessionmaker
from zenq.datapreparation.preparation import data_prep
import pandas as pd
import ipywidgets as widgets
from IPython.display import display
from sqlalchemy.exc import IntegrityError
from .config import db_uri
from zenq.logger import CustomFormatter, bcolors
import logging
import os
# from zenq.datapreparation.preparation import data_prep

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(funcName)s %(msg)s')
logger = logging.getLogger(os.path.basename(__file__))
# create console handler with a higher log level
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(CustomFormatter())
file_handler = logging.FileHandler('logs.log')
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(CustomFormatter())

logger.addHandler(file_handler)
logger.addHandler(ch)

engine = create_engine(db_uri)
Session = sessionmaker(bind=engine)
session = Session()

# def insert_facts(filename, customer_id, gender, invoice_id, date, quantity, total_price):
#     """

#     Parameters
#     ----------
#     filename :
        
#     customer_id :
        
#     gender :
        
#     invoice_id :
        
#     date :
        
#     quantity :
        
#     total_price :
        

#     Returns
#     -------

#     """
#     # Read the CSV file into a pandas DataFrame
#     df = pd.read_csv(filename)
#     print(f"Inserting facts for {customer_id} from file {filename}")
#     # Loop through the rows of the DataFrame and insert each one into the database
#     for i, row in df.iterrows():
#         fact = Facts(
#             customer_id=row[customer_id],
#             gender=row[gender],
#             invoice_id=row[invoice_id],
#             date=row[date],
#             quantity=row[quantity],
#             total_price=row[total_price]
#         )
#         try:
#             session.add(fact)
#             session.commit()
#         except IntegrityError:
#             session.rollback()
#             print(f"Skipping row with duplicate invoice_id: {row[invoice_id]}")
#             continue
#     print("Finished inserting facts")
#     session.close()
# data_prep = data_prep()
def insert_facts(customer_id, gender, invoice_id, date, quantity, total_price):
    # try:
        # Read the CSV file into a pandas DataFrame
    # df = pd.read_csv(filename)
    # except FileNotFoundError:
    #     print(f"Error: File {filename} not found. Please try again.")
    #     return
    # except pd.errors.EmptyDataError:
    #     print(f"Error: File {filename} is empty. Please try again.")
    #     return

    # Check if all required columns are present in the DataFrame
    df = data_prep.final_data()
    required_columns = [customer_id, gender, invoice_id, date, quantity, total_price]
    missing_columns = set(required_columns) - set(df.columns)
    if missing_columns:
        print(f"Missing columns: {missing_columns}")
        return

    print(f"Inserting facts for {customer_id} from file csv")
    # Loop through the rows of the DataFrame and insert each one into the database
    for i, row in df.iterrows():
        fact = Facts(
            customer_id=row[customer_id],
            gender=row[gender],
            invoice_id=row[invoice_id],
            date=row[date],
            quantity=row[quantity],
            total_price=row[total_price]
        )
        try:
            session.add(fact)
            session.commit()
        except IntegrityError:
            session.rollback()
            print(f"Skipping row with duplicate invoice_id: {row[invoice_id]}")
            continue
    print("Finished inserting facts")
           
    session.close()

    logger.error(f"{insert_facts.__name__}")
    logger.warning(f"{insert_facts.__name__}")  
    logger.info(f"{insert_facts.__name__}")    