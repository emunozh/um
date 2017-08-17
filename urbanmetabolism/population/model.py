#!/usr/bin/env python
# -*- coding:utf -*-
"""
#Created by Esteban.

Sun 18 Jun 2017 12:16:21 AM CEST

"""

# system libraries
import os
import warnings
import copy
warnings.filterwarnings('ignore')

# pymc3 libraries
from pymc3 import Model
from pymc3 import Normal, HalfNormal, Bernoulli, Beta, Bound, Poisson
from pymc3 import Deterministic, Categorical
from pymc3 import find_MAP, Metropolis, sample, trace_to_dataframe
from pymc3.backends import SQLite
import pymc3 as pm

# scientific python stack
from scipy import stats
from statsmodels.api import OLS, add_constant
from scipy.optimize import newton
import numpy as np
import pandas as pd
from pandas.core.dtypes.dtypes import CategoricalDtype
import seaborn as sns
import matplotlib.pyplot as plt
sns.set_context('notebook')

# theano libraries
from theano import config, function, shared
import theano.tensor as T
# import theano
# theano.config.compute_test_value = 'off'
config.warn.round = False
config.optimizer = 'None'
config.compute_test_value = "warn"

# rpy2 libraries
from rpy2.robjects.vectors import IntVector
from rpy2.robjects import DataFrame, pandas2ri
pandas2ri.activate()
from rpy2.robjects import r
from rpy2.robjects.packages import importr


#####################
## Global variables
#####################

PosNormal = Bound(Normal, lower=0, upper=np.inf)
Categorical_list = ['Categorical', 'Poisson', 'Bernoulli']


#####################
## Global functions
#####################

def _get_breaks(census_col):
    """Compute census breaks"""
    old_var_name = None
    breaks = list()
    for e, col in enumerate(census_col):
        var_name = col.split('_')[0]
        if var_name != old_var_name:
            if e-1 > 0:
                breaks.append(e-1)
            old_var_name = var_name
    breaks = breaks[:-1]
    breaks_r = IntVector(breaks)
    return(breaks_r)


def _toR_df(toR_df):
    """Convert pandas DataFrame to R data.frame."""
    for col in toR_df.columns:
        col_dtype = toR_df.loc[:, col].dtype
        if isinstance(col_dtype, CategoricalDtype):
            toR_df.loc[:, col] = toR_df.loc[:, col].astype(str)
    col = toR_df.columns.tolist()
    if 'Unnamed' in col[0]:
        col[0] = "X"
    toR_df.columns = col
    new_index = [i for i in range(1, toR_df.shape[0]+1)]
    toR_df.index = new_index
    toR_df = pandas2ri.py2ri(toR_df)
    return(toR_df, col)


def _script_gregwt(survey, census, weights_file, script):
    """Run GREGWT directly from an R script"""
    survey.to_csv('temp/toR_survey.csv')
    census.to_csv('temp/toR_census.csv')
    res = os.system("Rscript {}".format(script))
    if res != 0:
        raise ValueError("cannot run script: {}".format(script))
    new_weights = pd.read_csv(weights_file, index_col=0)
    return(new_weights)


def _gregwt(
    toR_survey, toR_census, pop_col = 'pop',
    verbose = False,
    log = True, complete = False,
    survey_index_col = True,
    census_index_col = True,
    **kwargs):
    """GREGWT."""
    if survey_index_col:
        sic = 2
    else:
        sic = 1
    if census_index_col:
        cic = 2
    else:
        cic = 1
    toR_census, census_col = _toR_df(toR_census)
    breaks_r = _get_breaks(census_col)
    toR_survey, _ = _toR_df(toR_survey)
    align_r = DataFrame({pop_col: IntVector((1, len(breaks_r)+2))})
    census_cat_r = IntVector([i for i in range(cic, toR_census.ncol)])
    survey_cat_r = IntVector([i for i in range(sic, toR_survey.ncol)])
    if verbose:
        print(survey_cat_r)
        print(toR_survey.colnames)
        print(toR_census.colnames)

    gregwt = importr('GREGWT')

    # (1) prepare data
    simulation_data = gregwt.prepareData(
        toR_census, toR_survey,
        align = align_r,
        breaks = breaks_r,
        survey_weights = 'w',
        pop_total_col = pop_col,
        census_categories = census_cat_r,
        survey_categories = survey_cat_r)

    # (2) reweight
    new_weights = gregwt.GREGWT(
        data_in = simulation_data, use_ginv = True, verbose = verbose,
        output_log = log, **kwargs)

    fw = new_weights.rx2('final_weights')
    if not complete:
        fw = fw.rx(True, sic)

    return(fw)


def _delete_files(name, sufix, verbose=False):
    files = "./data/trace_{0}_{1}.csv;./data/trace_{0}_{1}.sqlite".format(
        name, sufix)
    for file in files.split(";"):
        if os.path.isfile(file):
            if verbose:
                print("delete: ", file)
            os.remove(file)
        else:
            if verbose:
                print("no file: ", file)


def _project_survey_resample(
    trace, census, model_in, err,
    k_iter,
    **kwargs):
    """Project reweighted survey."""
    for year in census.index:
        print("resampling for year {}".format(year))
        sufix = "resample_{}".format(year)
        model = _make_flat_model(model_in, year)
        _, reweighted_survey = run_composite_model(
            model, sufix,
            year = year,
            err = err,
            k = k_iter,
            **kwargs)
        trace.loc[:, year] = reweighted_survey.loc[:, err]
    return(trace)


def plot_data_projection(reweighted_survey, var, iterations,
                         start_year = 2010, end_year = 2030, benchmark_year = False):
    """Plot projected data as total sum and as per capita."""
    if not isinstance(var, list):
        raise TypeError('expected type {} for variable var, got {}'.format(type(list), type(var)))
    inx = [str(i) for i in range(start_year, end_year)]
    for v in var:
        data = reweighted_survey.loc[:, inx].mul(
            reweighted_survey.loc[:, v], axis=0)
        cap = reweighted_survey.loc[:, inx].sum()
        _plot_data_projection_single(data, v, iterations, cap, benchmark_year)
    return(data)


def _plot_data_projection_single(data, var, iterations, cap, benchmark_year):
    """Plot projected data."""
    fig, ax1 = plt.subplots(figsize=(10,2))

    data.sum().plot(ax = ax1, label='total')
    ax1.set_xlabel('simulation year')
    if benchmark_year:
        ax1.set_ylabel('Total {}'.format(var))
        x = data.columns.tolist().index(str(benchmark_year))
        y = data.sum().loc[str(benchmark_year)]
        #ax1.arrow(x, 0, 0, y, head_width=1, head_length=1, fc='r', ec='r')
        ax1.vlines(x, 0, y, color='r')
        ax1.text(x+0.2, y/2, 'benchmark', color='r')
    ax1.legend(loc=2)

    ax2 = ax1.twinx()

    per_husehold = data.sum().div(cap)
    per_husehold.plot(style='k--', ax = ax2, label='per household\nsd={:0.2f}'.format(per_husehold.std()))
    ax2.set_title('{} projection $(n = {})$'.format(var, iterations))
    ax2.set_ylabel('{}\nHouseholds'.format(var))
    ax2.legend(loc=1)

    plt.savefig('FIGURES/projected_{}.png'.format(var), dpi=300)

def _project_survey_reweight(trace, census, model_i, err, max_iter = 100):
    """Project reweighted survey."""
    census.insert(0, 'area', census.index)

    drop_survey = [i for i in model_i]
    drop_survey.append(err)

    census_cols = [i.split('_')[0].lower() for i in census.columns]
    survey_cols = list()
    skip_cols = ['i', 'e', 'HH', 'head', 'cat']
    for i in trace.columns:
        survey_cols.extend(
            [j.lower().replace('urban', 'urbanity') for j in i.split('_') if j not in skip_cols])
    drop_census = [census.columns[e] for e, i in enumerate(census_cols)\
                   if i not in survey_cols and i not in ['area', 'pop']]

    survey_in = trace.loc[:, [i for i in trace.columns if i not in drop_survey]]
    census = census.loc[:, [i for i in census.columns if i not in drop_census and i not in drop_survey]]

    fw = _gregwt(survey_in, census, complete = True, area_code = 'internal', max_iter = max_iter)

    # index = [int(i) for i in fw.rx(True, 'id')]
    a = pd.DataFrame(index=trace.index)
    for e, i in enumerate([i for i in fw.colnames if 'id' not in i]):
        a.loc[:, i] = fw.rx(True, i)

    trace_out = trace.join(a)

    return(trace_out)


def plot_projected_weights(trace_out, iterations):
    """Plot projected weights."""
    fig, ax = plt.subplots(figsize=(8, 6))
    for c in trace_out.columns:
        try:
            year = int(c)
            if year >= 1900 and year < 3000:
                if c == '2016':
                    sc = 150
                    zo = 4
                else:
                    sc = 50
                    zo = 2
                ax.scatter(trace_out.loc[:, c], trace_out.wf, label=c, s=sc, zorder=zo)
        except:
            pass
    # ax.scatter(trace_out.w, trace_out.wf, label='w', s=150, zorder=3)
    lgd = ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.15), ncol=6);
    ax.set_title('Weights for each simulation year $(n = {})$'.format(iterations))
    ax.set_ylabel('Estimated final weights')
    ax.set_xlabel('Benchmarked weights to consumption $wf$')
    # plt.axis('equal')
    newax = fig.add_axes([0.7, 0.7, 0.2, 0.2], zorder=10)
    inx = [str(i) for i in range(2010, 2031)]
    trace_out.loc[:, inx].mean().plot(ax=newax)
    # newax.axis('off')
    newax.set_ylabel('mean weight')
    plt.tight_layout()
    plt.savefig('FIGURES/weight_mov_{}.png'.format(iterations),
                dpi=300, format='png', bbox_extra_artists=(lgd,), bbox_inches='tight')


def _make_flat_model(model, year):
    """make model flat."""
    model_out = copy.deepcopy(model)
    for mod in model_out:
        model_out[mod]['table_model'] = model_out[mod]['table_model'].loc[year]
    return(model_out)


def run_calibrated_model(model_in, log_level = 0, err = 'wf', project = 'reweight', **kwargs):
    """Run and calibrate model with all required iterations.

    Args:
        model_in (dict): Model defines as a dictionary, with specified variables
            as keys, containing the `table_model` for each variable and
            an optional formula.
        err (:obj:`str`): Weight to use for error optimization. Defaults to `'wf'`.
        log_level (:obj:`int`, optional): Logging level passed to pandas log-level.
        project (:obj:`str`): Method used for the projection of the sample survey.
            Defaults to `'reweight'`, this method will reweight the synthetic sample
            survey to match aggregates from the census file. This method is fast
            but might contain large errors on the resulting marginal sums (i.e. TAE).
            An alternative method is define as `'resample'`. This method will
            construct a new sample for each iteration and reweight it to the know
            aggregates on the census file, this method is more time consuming as
            the samples are created on each iteration via MCMC. If the variable
            is set to `False` the method will create a sample for a single year.
        **kwargs: Keyword arguments passed to 'run_composite_model'.

    Returns:
        reweighted_survey (pandas.DataFrame): calibrated reweighted survey.

    Example:
        >>> elec = pd.read_csv('data/test_elec.csv', index_col=0)
        >>> inc =  pd.read_csv('data/test_inc.csv',  index_col=0)
        >>> model = {"Income":      {'table_model': inc },
                     "Electricity": {'table_model': elec}
        >>> reweighted_survey = run_calibrated_model(
                model,
                name = 'Sorsogon_Electricity',
                population_size = 32694,
                iterations = 100000)

    """
    if 'name' in kwargs:
        model_name = kwargs['name']
    else:
        print("No model name provided!")
        model_name = 'noname'

    if 'year' in kwargs:
        year_in = kwargs.pop('year')
    else:
        year_in = 2010
        print("Warning: using default year <{}> as benchmark year".format(year_in))

    if 'verbose' in kwargs:
        verbose = kwargs['verbose']
        print('Being verbose')
    else:
        verbose = False

    if any([isinstance(model_in[i]['table_model'], pd.Panel) for i in model_in]):
        if verbose:
            print("Model define as dynamic.")
        model = _make_flat_model(model_in, year_in)
    else:
        model = model_in

    if verbose:
        for mod in model:
            print("#"*30)
            print(mod)
            print("#"*30)
            print(model[mod]['table_model'])

    pm._log.setLevel(log_level)
    k_iter = {i:1 for i in model}
    n_models = len(model.keys()) + 1
    for e, variable in enumerate(model):
        sufix = "loop_{}".format(e+1)
        print('loop: {}/{}; calibrating: {}; sufix = {}'.format(
            e+1, n_models, variable, sufix))
        k_out, _ = run_composite_model(
            model, sufix,
            year = year_in,
            err = err,
            k = k_iter,
            **kwargs
        )
        k_iter[variable] = k_out[variable]

    sufix = "loop_{}".format(e+2)
    print('loop: {}/{}; final loop, for variables: {}; sufix = {}'.format(
        e+2, n_models, ", ".join([v for v in model]), sufix))
    _, reweighted_survey = run_composite_model(
        model, sufix,
        year = year_in,
        err = err,
        k = k_iter,
        **kwargs)

    print("Calibration Error:")
    for md in k_out:
        print('\t{1:0.4E}  {0:}'.format(md, 1 - k_out[md]))

    census_file = kwargs['census_file']
    census = pd.read_csv(census_file, index_col=0)
    if census.shape[0] > 1 and project == 'reweight':
        print("Projecting sample survey for {} steps via reweight".format(
            census.shape[0]))
        out_reweighted_survey = _project_survey_reweight(
            reweighted_survey, census, model, err)
    elif census.shape[0] > 1 and project == 'resample':
        print("Projecting sample survey for {} steps via resample".format(
            census.shape[0]))
        out_reweighted_survey = _project_survey_resample(
            reweighted_survey, census, model_in, err,
            k_iter, **kwargs)
    else:
        out_reweighted_survey = reweighted_survey

    out_reweighted_survey = out_reweighted_survey.set_index('index')
    out_reweighted_survey.to_csv("./data/survey_{}.csv".format(model_name))
    return(reweighted_survey)


def run_composite_model(
    model, sufix,
    population_size = 1000,
    err = 'wf',
    iterations = 100,
    name = 'noname',
    census_file = 'data/benchmarks.csv',
    drop_col_survey = False,
    verbose = False,
    from_script = False,
    k = dict(),
    year = 2010,
    to_cat = False,
    reweight = True):
    """Run and calibrate a single composite model.

    Args:
        model (dict): Dictionary containing model parameters.
        sufix (str): model `name` sufix.
        name (:obj:`str`, optional). Model name. Defaults to `'noname'`.
        population_size (:obj:`int`, optional). Total population size.
            Defaults to 1000.
        err (:obj:`str`): Weight to use for error optimization. Defaults to `'wf'`.
        iterations (:obj:`int`, optional): Number of sample iterations on MCMC model.
            Defaults to `100`.
        census_file (:obj:`str`, optional): Define census file with aggregated
            benchmarks. Defaults to `'data/benchmarks.csv'`.
        drop_col_survey (:obj:`list`, optional): Columns to drop from survey.
            Defaults to `False`.
        verbose (:obj:`bool`, optional): Be verbose. Defaults to `Fasle`.
        from_script (:obj:`bool`, optional): Run reweighting algorithm from file.
            Defaults to `Fasle`.
        k (:obj:`dict`, optional): Correction k-factor. Default 1.
        year (:obj:`int`, optional): year in `census_file` (i.e. index) to use for the model califration `k` factor.
        to_cat (:obj:`bool`, optional): Convert survey variables to categorical
            variables. Default to `False`.
        reweight(:obj:`bool`, optional): Reweight sample. Default to `True`.

    Returns:
        result (:obj:`list` of :obj:`objects`): Returns a list containing the
            estimated k-factors as `model.PopModel.aggregates.k` and the reweighted
            survey as `model.PopModel.aggregates.survey`.

    Examples:

        >>> k_out, reweighted_survey = run_composite_model(model, sufix)


    """
    if verbose:
        print("#"*50)
        print("Start composite model: ", name, sufix)
        print("#"*50)

    _delete_files(name, sufix, verbose = verbose)

    for mod in model:
        if verbose:
            print('k for {:<15}'.format(mod), end='\t')
        try:
            k[mod] = float(k[mod])
        except:
            k[mod] = 1.0
        if verbose:
            print(k[mod])
    if not all([isinstance(i, float) for i in k.values()]):
        raise TypeError('k values must be float values.')

    m = PopModel('{}_{}'.format(name, sufix), verbose = verbose)
    for mod in model:
        try:
            formula = model[mod]['formula']
        except:
            if verbose:
                print('no defined formula for: ', mod)
            formula = False
        m.add_consumption_model(
            mod, model[mod]['table_model'],
            formula = formula,
            prefix = mod[0].lower(),
            k_factor = k[mod])

    # define Aggregates
    m.aggregates.set_table_model([m._table_model[i] for i in m._table_model])
    m.aggregates._set_census_from_file(
        census_file,
        total_pop = population_size,
        index_col = 0)
    population_size_census = m.aggregates.census.loc[year, m.aggregates.pop_col]
    # run the MCMC model
    m.run_model(iterations = iterations, population = population_size_census)
    # define survey for reweight from MCMC
    if verbose:
        print("columns of df_trace:")
        print(m.df_trace.columns)
    m.aggregates._set_survey_from_frame(
        m.df_trace,
        drop = drop_col_survey,
        to_cat = to_cat)

    if verbose:
        print("columns of m.aggregates.survey:")
        print(m.aggregates.survey.columns)
        for mod in model:
            print("Error for mod: ", mod)
            _ = m.aggregates.print_error(mod, "w", year = year)
    drop_cols = [i for i in model]
    m.aggregates.reweight(drop_cols, from_script = from_script, year = year)
    for mod in model:
        m.aggregates.compute_k(year = year, var = mod, weight = err)
    if verbose:
        print("columns of m.aggregates.survey after reweight:")
        print(m.aggregates.survey.columns)
        for mod in model:
            _ = m.aggregates.print_error(mod, err, year = year)
    return(m.aggregates.k, m.aggregates.survey)


def _make_theano(co_mu_list):
    co_mu_list = shared(np.asarray(
        co_mu_list, dtype = config.floatX), borrow = True)
    co_mu_list = co_mu_list.flatten()
    co_mu_list = T.cast(co_mu_list, 'float64')
    return(co_mu_list)


def _make_theano_var(variable, var_typ):
    if var_typ == 'float64':
        variable = np.float64(variable)
    var_out = T.cast(variable, var_typ)
    return(var_out)


def _index_model(inx, co_mu_list):
    res = co_mu_list[inx]
    return(res)


#####################
## Table model
#####################

class TableModel(object):
    """Static and dynamic table model."""
    def __init__(
        self,
        census_file = False,
        verbose=False):

        if census_file:
            if not os.path.isfile(census_file):
                raise ValueError("{} not a file".format(census_file))
            self.census = pd.read_csv(census_file, index_col=0)
            self.dynamic = True
        else:
            self.dynamic = False

        self.models = dict()
        self.formulas = dict()
        self.skip = ['cat', 'Intercept']
        self.verbose = verbose

    def make_model(self):
        """prepare model for simulation."""
        model_out = dict()
        for name in self.models:
            model_out[name] = {'table_model': self.models[name]}
            try:
                formula = self.formulas[name]
                model_out[name]['formula'] = formula
            except:
                pass
        return(model_out)

    def print_formula(self, name):
        """pretty print table_model formula."""
        print(name, "=")
        for f in self.formulas[name].split('+'):
            print("\t", f, "+")

    def add_formula(self, formula, name):
        """add formula to table model."""
        self.formulas[name] = formula

    def add_model(self, table, name, index_col = 0, **kwargs):
        """Add table model."""
        if self.verbose:
            print('adding {} model'.format(name), end=' ')
        table = pd.read_csv(table, index_col=index_col, **kwargs)
        self.models[name] = table
        if self.dynamic:
            if self.verbose: print("as dynamic model.")
            self.update_dynamic_model(name)
        else:
            if self.verbose: print("as static model.")

    def update_dynamic_model(self, name,
                             val = 'p',
                             specific_col = False,
                             compute_average = False):
        """Update dynamic model."""
        table = self.models[name]
        if specific_col:
            if self.verbose: print("\t| for specific column {}".format(specific_col))
            v_cols = [i for i in self.census.columns if specific_col in i or specific_col.lower() in i]
            if self.verbose:
                print('specific col: ', end='\t')
                print(v_cols)
        else:
            if self.verbose: print("\t| for all columns:")
            v_cols = self._get_cols(table)
            if self.verbose:
                for col in v_cols:
                    print("\t\t| {}".format(col))

        panel_dic = dict()
        for year in self.census.index:
            if self.verbose: print("\t|", year, end="\t")
            if isinstance(table, pd.DataFrame):
                if self.verbose: print('| table is DataFrame')
                this_df = table.copy()
            elif isinstance(table, pd.Panel):
                if self.verbose: print('| table is Panel')
                this_df = table[year].copy()
            else:
                print('unimplemented type: ', type(table))

            this_df = self._update_table(
                this_df, year,
                v_cols, val, name, specific_col,
                compute_average)
            panel_dic[year] = this_df

        self.models[name] = pd.Panel(panel_dic)

    def _update_table(self, this_df, year, v_cols, val,
                      name, specific_col, compute_average):
        new_val = False
        prefix = name[0].lower()
        if specific_col:
            e1, _, _ = self._get_positions(specific_col, prefix)
            if not isinstance(compute_average, bool):
                new_val = self._get_weighted_mean(v_cols, year)
                new_val += compute_average
                if self.verbose:
                    print('\t\t\t| computed average:')
                    print('\t\t\t| {}_{:<18} {:0.2f}'.format(
                        prefix, e1, new_val))
            else:
                new_val = ','.join(
                    [str(i) for i in self.census.loc[year, v_cols].div(
                        self.census.loc[year, 'pop'])])
                if self.verbose:
                    print('\t\t\t| categorical values:')
                    print('\t\t\t| {}_{:<18} {}'.format(
                        prefix, e1, new_val))
            if new_val:
                this_df.loc['{}_{}'.format(prefix, e1), val] = new_val
        else:
            for e in v_cols:
                e1, e2, sufix = self._get_positions(e, prefix)
                val_a = self.census.loc[year, '{}_{}'.format(e2, sufix)]
                val_b = self.census.loc[year, 'pop']
                new_val = val_a / val_b
                if self.verbose:
                    print('\t\t\t| {}_{:<18} {:8.2f} / {:8.2f} = {:0.2f}'.format(
                        prefix, e1,
                        val_a, val_b,
                        new_val,
                        ), end='  ')
                    print('| {}_{}'.format(e2, sufix))
                this_df.loc['{}_{}'.format(prefix, e1), val] = new_val

        return(this_df)

    def _get_weighted_mean(self, inx, year):
        x = [int(i.split('_')[-1]) for i in inx]
        w = self.census.loc[year, inx].tolist()
        avr = np.average(x, weights=w)
        return(avr)

    def _get_positions(self, e, prefix):
        sufix = ''; e2 = ''
        if e == 'Urban' or e == 'Urbanity':
            sufix = 'Urban'
            e2 = 'Urbanity'
        elif e == 'Sex':
            sufix = 'female'
            e2 = 'sex'
            e = 'HH_head_Sex'
        elif e == 'Size':
            e = 'Family_Size'
        elif e == 'Age':
            e = 'HH_head_Age'
        elif e == 'Education':
            e = 'Education_cat'
        else:
            sufix = 'yes'
            e2 = e
        return(e, e2, sufix)

    def _get_cols(self, table, val = 'p'):
        cols = [el.split('_')[-1] for el in table.index if \
                el.split('_')[-1] not in self.skip and \
                not np.isnan(float(table.loc[el, val]))
               ]
        return(cols)


#################
## PopModel
################

class PopModel(object):
    """Main population model class."""
    def __init__(self, name='noname', verbose=False, random_seed=12345):
        """Class initiator"""

        self._command_no = "{0} = Normal('{0}', mu={1}, sd={2}); "
        self._command_pn = "{0} = PosNormal('{0}', mu={1}, sd={2}); "
        self._command_br = "{0} = Bernoulli('{0}', {1}); "
        self._command_bt = "{0} = Beta('{0}', {1}, {2}); "
        self._command_ps = "{0} = Poisson('{0}', {1}); "
        self._command_ct = "{0} = Categorical('{0}', p=np.array([{1}])); "
        self._command_dt = "{0} = Deterministic('{0}', {1}); "

        self.name = name
        self._table_model = dict()
        self._model_bounds = dict()
        self.basic_model = Model()
        self.command = ""
        self.pre_command = ""
        self.tracefile = os.path.join(
            os.getcwd(),
            "data/trace_{}.sqlite".format(self.name))
        self.mu = dict()
        self.regression_formulas = dict()
        self._models = 0
        self.verbose = verbose
        self.random_seed = random_seed
        self.aggregates = Aggregates(verbose = verbose)

    def _get_distribution(self, dis, var_name, p):
        """Get distribution."""
        if self.verbose:
            print('computing for distribution: ', dis)
        if 'None' in dis:
            l = ''
        elif dis == 'Normal':
            l = self._command_no.format(var_name, p['mu'], p['sd'])
        elif dis == 'Beta':
            l = self._command_bt.format(var_name, p['alpha'], p['beta'])
        elif dis == 'Bernoulli':
            l = self._command_br.format(var_name, p['p'])
        elif dis == 'PosNormal':
            l = self._command_pn.format(var_name, p['mu'], p['sd'])
        elif dis == 'Poisson':
            l = self._command_ps.format(var_name, p['mu'])
        elif dis == 'Categorical':
            l = self._command_ct.format(var_name, p['p'])
        elif dis == 'Deterministic':
            p_in = p['p']
            l = self._command_dt.format(var_name, p_in)
        else:
            raise ValueError('Unknow or unspecified distribution: {}'.format(dis))
        return(l)

    def _call_gregwt(self):
        """Call GREGWT on computed sample."""
        pass

    def _make_regression_formula(self, yhat_name, table_model,
                                 formula=False,
                                 constant_name='Intercept'):
        """Construct regression formula for consumption model."""
        if not formula:
            exclusion = [yhat_name, constant_name]
            estimators = list()
            for i in table_model.index:
                if i not in exclusion:
                    if table_model.loc[i, 'dis'] in Categorical_list:
                        estimators.append("C({})".format(i))
                    else:
                        estimators.append(i)
        else:
            elements = formula.split(',')
            estimators = []
            for e in elements:
                for f in e.split('+'):
                    for g in f.split('*'):
                        if 'c_' not in g:
                            estimators.append(g.strip())
        estimators = ' + '.join(estimators)
        regression_formula = '{} ~ {}' .format(yhat_name, estimators)

        return(regression_formula)

    def _make_categories_formula(self, p, var_name, index_var_name):
        """Construct formula for categorical variables."""
        list_name = "c_{}_list".format(var_name)
        self.pre_command += "{} = [{}];".format(list_name, p['co_mu'])
        self.pre_command += "{0} = _make_theano({0});".format(list_name)
        c = ''
        if not index_var_name:
            index_var_name = var_name
        var = "_index_model({}, {})".format(index_var_name, list_name)
        var_dic = {'p': var}
        c += self._get_distribution(
            'Deterministic',
            'c_'+var_name,
            var_dic)

        return(c)

    def _make_linear_model(self, constant_name, yhat_name, formula):
        """Make linear model."""
        table_model = self._table_model[yhat_name]
        linear_model = "yhat_mu_{} = ".format(self._models)
        if not formula:
            formula = constant_name+"+"+"+".join(
                ["c_{0}*{0}".format(e) for e in table_model.index if\
                    (e != constant_name) &\
                    (table_model.loc[e, 'dis'] != "Categorical") &\
                    (table_model.loc[e, 'dis'] != "Deterministic")
                 ])
            for deter in table_model.loc[table_model.dis == 'Categorical'].index:
                formula += "+c_{}".format(deter)
        linear_model += formula

        for var_name in table_model.index:
            p = table_model.loc[var_name]
            dis = p['dis']
            if var_name == constant_name:
                command_var = "intercept_{} = _make_theano_var({}, 'float64');".format(
                    self._models, p['p'])
                self.pre_command += command_var
                p_in = {'p':'intercept_{}'.format(self._models)}
                l = self._get_distribution(dis, var_name, p_in)
            else:
                l = self._get_distribution(dis, var_name, p)
            self.command += l
            try:
                dis_split = dis.split(';')
                dis = dis_split[-1].strip()
                index_var_name = dis_split[-2] + '_' + "_".join(var_name.split('_')[1:])
            except:
                index_var_name = False
            if var_name != constant_name:
                if dis != 'Categorical':
                    this_mu = p['co_mu']
                    this_sd = p['co_sd']
                    c = self._command_no.format('c_'+var_name, this_mu, this_sd)
                else:
                    c = self._make_categories_formula(p, var_name, index_var_name)
            else:
                c = ''
            self.command += c

        return(linear_model)

    def print_command(self):
        """Print computed command."""
        print("The define model will be executed with the following commands:")
        print(self.pre_command.replace(';', '\n'))
        print(self.command.replace('; ', '\n'))

    def add_consumption_model(self, yhat_name, table_model,
                              k_factor=1,
                              sigma_mu=False, sigma_sd=0.1,
                              prefix = False,
                              bounds = [0, np.inf],
                              constant_name = 'Intercept',
                              formula=False):
        """Define new base consumption model.
        """
        if prefix:
            constant_name = "{}_{}".format(prefix, constant_name)
        self.regression_formulas[yhat_name] = self._make_regression_formula(
            yhat_name, table_model,
            formula=formula, constant_name=constant_name)
        self._table_model[yhat_name] = table_model
        self._model_bounds[yhat_name] = bounds
        self._models += 1
        self.mu[yhat_name] = sigma_mu
        linear_model = self._make_linear_model(constant_name, yhat_name, formula)
        self.command += linear_model
        self.command += '; '
        self.command += "yhat_mu_{0} *= _make_theano({1}); ".format(
            self._models, k_factor)
        if sigma_mu:
            # Estimate var consumption with a normal distribution
            self.command += self._command_no.format(
                'sigma_{}'.format(self._models), sigma_mu, sigma_sd)
            self.command += self._command_no.format(
                yhat_name,
                'yhat_mu_{}'.format(self._models),
                'sigma_{}'.format(self._models))
        else:
            # Estimate deterministically
            self.command += self._command_dt.format(
                yhat_name,
                'yhat_mu_{}'.format(self._models)
            )

    def run_model(self, iterations=100000, population=False, burn=False, thin=2, **kwargs):
        """Run the model.
        """
        if not burn:
            burn = iterations * 0.01
        if not population:
            population = iterations
        iterations += burn
        iterations *= thin
        if self.verbose:
            print('will save the data to ', self.tracefile )

        if self.verbose:
            print(self.pre_command.replace(";", "\n"))
            print(self.command.replace("; ", "\n"))

        exec(self.pre_command)
        with self.basic_model:
            exec(self.command)
            # obtain starting values via MAP
            # start = find_MAP()

            # Use the Metropolis algorithm (as opposed to NUTS or HMC, etc.)
            # step = Metropolis()
            # means, sds = pm.variational.advi(n=iterations*2)
            # step = pm.NUTS(scaling=means)

            # use SQLite as a backend
            backend = SQLite(self.tracefile)

            # Calculate the trace
            self.trace = sample(
                iterations,
                # step,
                # start=means,
                # step, start,
                trace=backend,
                random_seed=self.random_seed,
                **kwargs
            )

        # Transform data to DataFrame
        self.trace = self.trace[int(burn)::int(thin)]
        self.df_trace = trace_to_dataframe(self.trace)
        new_col = [col.replace('__0', '') for col in self.df_trace.columns]
        self.df_trace.columns = new_col

        # Truncate values
        self._truncate()
        self.df_trace = self.df_trace.dropna()
        self.df_trace = self.df_trace.reset_index()

        # Add initial weight to trace
        weight_factor = population / self.df_trace.shape[0]
        self.df_trace.loc[:, 'w'] = weight_factor

        # Save values
        self._save_trace()

    def _save_trace(self):
        """Save trace as csv file."""
        csvfile = self.tracefile.split('.')[0]
        csvfile += '.csv'
        self.df_trace.to_csv(csvfile)

    def _truncate(self):
        """Truncate distributions."""
        for yhat_name in self._table_model:
            table_model = self._table_model[yhat_name]
            bounds = self._model_bounds[yhat_name]
            if not np.isnan(bounds[0]) or not np.isnan(bounds[1]):
                self._truncate_single(table_model, yhat_name, bounds)
            for variable in table_model.index:
                var_bounds = table_model.loc[variable, ['lb', 'ub']]
                if not np.isnan(var_bounds['lb']) or not np.isnan(var_bounds['ub']):
                    self._truncate_single(table_model, variable, var_bounds.tolist())

    def _truncate_single(self, table_model, variable, bounds):
        """Truncate trace values."""
        truncated_trace = self.df_trace
        if variable in truncated_trace.columns:
            if self.verbose:
                print("bounds: {} for var: {}".format(bounds, variable))
                print("start size:", self.df_trace.shape[0])
                print("mean: {:0.2f}".format(self.df_trace.loc[:, variable].mean()))
            inx = (truncated_trace.loc[:, variable] >= bounds[0]) &\
                  (truncated_trace.loc[:, variable] <= bounds[1])
            truncated_trace = truncated_trace.loc[inx]
        else:
            print("can't find variable: {} on trace".format(variable))
        self.df_trace = truncated_trace
        if variable in truncated_trace.columns:
            if self.verbose:
                print("final size: ", self.df_trace.shape[0])
                print("mean: {:0.2f}".format(
                    self.df_trace.loc[:, variable].mean()))

    def _compute_initial_weights(self):
        """Compute the initial weight factorto mathc total population."""
        pass

    def _add_initial_weights(self):
        """Add initial weights to sample."""
        pass

    def plot_model(self):
        """Model traceplot."""
        from pymc3 import traceplot
        traceplot(self.trace);
        plt.show()

    def plot_model_test(self, yhat_name, mu, sd):
        """Model test plot"""
        from scipy import stats
        data = self.df_trace.loc[:, yhat_name]
        x = np.arange(data.min(), data.max())
        pdf_fitted = stats.norm.pdf(x, mu, sd)
        g = sns.distplot(data, label="Posterior (MCMC)")
        g.set_ylabel('households')
        g.set_xlabel(yhat_name)
        ax = g.twinx()
        ax.plot(pdf_fitted, color='r',
            label="Normal Distribution:\
                   \n$\mu={:0.0f}$; $\sigma={:0.0f}$".format(mu, sd))
        ax.set_ylabel("Density")
        lines, labels = ax.get_legend_handles_labels()
        lines2, labels2 = g.get_legend_handles_labels()
        ax.legend(lines + lines2, labels + labels2, loc=0)
        plt.show()


#####################
## Aggregates class
#####################

class Aggregates():
    """Class containing the aggregated data.

    Args:
        verbose (:obj:`bool`, optional): Defaults to `False`.
        pop_col (:obj:`str`, optional): Defaults to `'pop'`.

    Attributes:
        pop_col (str): Population column on census data.
        k (dict): Dictionary containing the k-factors.
        inverse = (list): List of categories to invert.
        verbose (bool): Be verbose.

    """

    def __init__(self, verbose = False, pop_col = 'pop'):
        """Class initiator"""
        self.pop_col = pop_col
        self.verbose = verbose
        self.k = dict()
        self.inverse = []

    def compute_k(self, init_val = False,
                  inter = 'Intercept', prefix = 'e_',
                  year = 2010, weight = 'wf',
                  var='Electricity'):
        """Compute the k factor to minimize the error using the Newton-Raphson algorithm.

        Args:
            init_val (:obj:`float`, optional): Estimated initial value passed
                to the Newton-Raphson algorithm for optimizing the error value.
                Defaults to `False`. If no value is given the function will try
                get the intercept value from the `table_model`. If the function
                does not find the intercept value on the `table_model` it will
                set it to 1.
            inter (:obj:`str`, optional): Intercept name on `table_model`.
                Defaults to `'Intercept'`.
            prefix (:obj:`str`, optional): Model prefix. Defaults to `'e_'`.
            year (:obj:`int`, optional): year to be used from aggregates.
                Defaults to `2010`.
            weight (:obj:`str`, optional): Weight value to use for optimizations.
                Defaults to `wf`.
            var (:obj:`str`, optional): Variable to minimize the error for.
                Defaults to `'Electricity'`.

        """
        if not init_val:
            try:
                for inx in self.table_model.index.tolist():
                    if inter in inx and prefix in inx:
                        init_val = self.table_model.loc[inx, 'p']
            except:
                init_val = 1
        k = newton(self._compute_error, init_val, args=(var, weight, year))
        self.k[var] = k

    def _compute_error(self, k, var, weight, year):
        """compute error on marginal sums."""
        if np.isnan(self.census.loc[year, var]):
            if self.verbose:
                print("Warning: can't compute error for year {}".format(year))
                print("Error will be set to 0 for the Newton-Raphson optimization algorithm to converge")
            return(0)
        error = (
            self.survey.loc[:, var].mul(self.survey.loc[:, weight]).mul(k).sum() -\
            self.census.loc[year, var])
        return(error)

    def print_error(self, var, weight, year = 2010, lim = 1e-6):
        """Print computed error to command line.

        Args:
            var (str): Variable name.
            weight (str): Weight variable to use for the estimation of error.
            year (:obj:`int`, optional): Year to compute the error for. Defaults to `2010`.
            lim (:obj:`float`, optional): Limit for model to converge. Defaults to `1e-6`.

        Returns:
            error (float): Computed error as:
                :math:`\sum_i X_{i, var} * w_i * k_{var} - Tx_{var, year}`

            Where:
                - :math:`Tx` are the known marginal sums for variable :math:`var`.
                - :math:`X` is the generated sample survey. For each :math:`i` record on the sample of variable :math:`var`.
                - :math:`w` are the estimated new weights.
                - :math:`k` estimated correction factor for variable :math:`var`.

        """
        try:
            k = self.k[var]
        except:
            k = 1
        error = self._compute_error(k, var, weight, year)
        print("error: {:0.2E} for {}".format(error, var), end='\t')
        if error > 0:
            print("Overestimate!")
        else:
            print("Underestimate!")
        if abs(error) <= lim:
            print("Model Converged, error lover than {}".format(lim))
        return(error)



    def reweight(self, drop_cols, from_script=False, year = 2010,
                 max_iter = 100,
                 weights_file='temp/new_weights.csv', script="reweight.R"):
        """Reweight survey using GREGWT.

        Args:
            drop_cols (list): list of columns to drop previous to the reweight.
            from_script (:obj:`bool`, optional): runs the reweight from a script.
            script (:obj:`str`, optionsl): script to run for the reweighting of
                the sample survey. `from_script` nees to be set to `True`. Defaults to `'reweight.R'`
            weights_file (:obj:`str`, optional) file to store the new weights.
                Only required if reweight is run from script.
                Defaults to `'temp/new_weights.csv'`

        """
        self.survey = self.survey.reset_index()
        self.survey = self.survey.loc[:, [i for i in self.survey.columns if i not in 'level_0']]
        inx_cols_survey = [col for col in self.survey.columns if col not in drop_cols and col not in 'level_0']
        inx_cols_census = [col for col in self.census.columns if col not in drop_cols]
        toR_survey = self.survey.loc[:, inx_cols_survey]
        toR_census = self.census.loc[[year], inx_cols_census]
        toR_census.insert(0, 'area', toR_census.index)

        if from_script:
            new_weights = _script_gregwt(toR_survey, toR_census,
                                         weights_file, script)
        else:
            new_weights = _gregwt(toR_survey, toR_census,
                                  verbose = self.verbose, max_iter = max_iter)

        if from_script:
            if new_weights.shape[0] == self.survey.shape[0]:
                new_weights = new_weights.reset_index()
                self.survey.loc[:, 'wf'] = new_weights.ix[:, -1]
            else:
                raise ValueError("weights length and sample size differ")
        else:
            if len(new_weights) == self.survey.shape[0]:
                self.survey.loc[:, 'wf'] = new_weights
            else:
                raise ValueError("weights length and sample size differ")


    def _is_inv(self, var):
        """Reverse list for inverse categories."""
        if var in self.inverse:
            inverse_e = -1
        else:
            inverse_e = 1
        return(inverse_e)

    def _match_keyword(self, var):
        """Match survey and census categories by keywords"""
        labels = list()
        for var_split in var.split("_"):
            if (len(var_split) > 1) & (var_split != 'cat'):
                for ms in self.census.columns:
                    if var_split.lower() in ms.lower():
                        labels.append(ms)
        return(labels)

    def _match_labels(self, labels, cat, inv=1):
        """match labels to census variables."""
        if self.verbose:
            print("matching categories: ", cat)
        cat = np.asarray(cat)
        labels = labels[::inv]
        if self.verbose:
            print('to labels: ', labels)
        labels = [labels[int(i)] for i in cat]
        return(labels)

    def _get_labels(self, var, cat, inv=1):
        """Get labels for survey variables."""
        n_cat = cat.shape[0]
        labels = self._match_keyword(var)
        if n_cat != len(labels):
            if self.verbose:
                print("\t|Warning: {} categories on marginal sums\
                      \n\t\tbut only {} on sample for variable {}".format(
                        len(labels), n_cat, var))
            labels = self._match_labels(labels, cat.tolist(), inv=inv)
            return(labels)
        else:
            return(labels)

    def _survey_to_cat_single(self, variable_name, cut_values, labels=False, prefix=False):
        """Transform single variable to categrical."""
        if not labels:
            labels = list()
            if prefix:
                prefix = "{}_".format(prefix)
            else:
                prefix = ''
            for cut_l in range(len(cut) -1):
                labels.append("{}{}-{}".format(prefix, cut[cut_l], cut[cut_l +1]))

        self.survey.loc[:, variable_name] = pd.cut(
            self.survey.loc[:, variable_name],
            cut_values,
            right=False,
            labels=labels)

    def _survey_to_cat(self):
        """Convert survey values to categorical values."""
        for var in self.survey.columns:
            if self.verbose:
                print("processing var: ", var, end='\t')
            try:
                dis = self.table_model.loc[var, 'dis']
                if self.verbose:
                    print('OK')
            except:
                if self.verbose:
                    print('Fail!, no defined distribution.')
                dis = ''
            if dis in Categorical_list:
                if self.verbose:
                    print('\t|Distribution defined as categorical')
                inv = self._is_inv(var)
                self.survey.loc[:, var] = self.survey.loc[:, var].astype('category')
                cat = self.survey.loc[:, var].cat.categories
                if len(cat) > 1:
                    labels = self._get_labels(var, cat, inv=inv)
                    self.survey.loc[:, var] = self.survey.loc[:, var].cat.rename_categories(labels)
                else:
                    if self.verbose:
                        print("\t\t|Single category, won't use variable: ", var)
                    self.survey = self.survey.loc[:, [i for i in self.survey.columns if i != var]]
                    columns_delete = self._match_keyword(var)
                    self.census = self.census.loc[:,[i for i in self.census.columns if i not in columns_delete]]
            else:
                if self.verbose:
                    print("\t|Warning: distribution <{}> of var <{}> not defined as categorical".format(dis, var))

    def set_table_model(self, input_table_model):
        """define table_model.

        Args:
            input_table_model (list, pandas.DataFrame): input `table_model` either as a list of `pandas.DataFrame` or as a single `pandas.DataFrame`.

        Raises:
            ValueError: if `input_table_model` is not a list of `pandas.DataFrame` or a single `pandas.DataFrame`.

        """
        if isinstance(input_table_model, list):
            data_frame = pd.concat(input_table_model)
        elif isinstance(input_table_model, pd.DataFrame):
            data_frame= input_table_model
        else:
            raise ValueError('can convert data type {} to table_model'.format(
                type(input_table_model)))
        self.table_model = data_frame

    def _cut_survey(self, drop = False):
        """drop unwanted columns from survey"""
        inx_data = [c for c in self.survey.columns if\
                    ('c_' not in c) and \
                    ('sigma' not in c) and \
                    ('Intercept' not in c)]
        if drop:
            for d in drop:
                inx_data = [c for c in inx_data if d != c]
        inx_coef = [c for c in self.survey.columns if ('c_' in c)]
        self.coefficients = self.survey.loc[:, inx_coef]
        # if self.verbose: print("_cut_survey:"); print(self.survey.columns)
        self.survey = self.survey.loc[:, inx_data]
        if self.verbose: print("_cut_survey:"); print(self.survey.columns)
        self._survey_to_cat()
        if self.verbose: print("_cut_survey:"); print(self.survey.columns)

    def set_survey(self, survey,
                   inverse = False, drop = False, to_cat = False, **kwargs):
        """define survey.

        Args:
            survey (str, pandas.DataFrame): Either survey data as
                `pandas.DataFrame` or name of a file as `str`.
            inverse (:obj:`bool`, optional): Defaults to `False`.
            drop: (:obj:`bool`, optional): Defaults to `False`.
            to_cat: (:obj:`bool`, optional): Defaults to `False`.
            **kwargs: Optional kword arguments for reading data from file,
                only used if `survey` is a file.

        Raises:
            TypeError: If `survey` is neither not a string or a DataFrame.
            ValueError: If `survey` is not a valid file.

        """
        if isinstance(survey, pd.DataFrame):
            self._set_survey_from_frame(
                survey,
                inverse = inverse, drop = drop, to_cat = to_cat)
        elif isinstance(survey, str):
            if not os.path.isfile(survey):
                raise ValueError("Can't find file {} on disk".format(survey))
            self._set_survey_from_file(
                survey,
                inverse = inverse, drop = drop, to_cat = to_cat, **kwargs)
        else:
            raise TypeError("survey must be either a path, formated as str or a pandas DataFrame. Got: {}".format(type(survey)))

    def set_census(self, census, total_pop = False, **kwargs):
        """define census.

        Args:
            census (str, pandas.DataFrame): Either census data as `pandas.DataFrame` or name of a file as `str`.
            total_pop (:obj:`int`, optional): Total population. Defaults to `False`.
            **kwargs: Optional kword arguments for reading data from file,
                only used if `census` is a file.

        Raises:
            TypeError: If `census` is neither not a string or a DataFrame.
            ValueError: If `census` is not a valid file.

        """
        if isinstance(census, pd.DataFrame):
            self._set_census_from_frame(survey, total_pop = total_pop)
        elif isinstance(survey, str):
            if not os.path.isfile(survey):
                raise ValueError("Can't find file {} on disk".format(survey))
            self._set_census_from_file(survey, total_pop = total_pop, **kwargs)
        else:
            raise TypeError("census must be either a path, formated as str or a pandas DataFrame. Got: {}".format(type(census)))

    def _set_survey_from_file(self, file_survey,
                             inverse = False, drop = False, to_cat = False, **kwargs):
        """define survey from file"""
        if inverse:
            self.inverse = inverse
        self.survey = pd.read_csv(file_survey, **kwargs)
        self._cut_survey(drop=drop)
        if to_cat:
            self._add_cat(to_cat)

    def _add_cat(self, to_cat):
        """Add category to survey from dict."""
        for key in to_cat:
            if isinstance(to_cat[key], list):
                self._survey_to_cat_single(
                    key, to_cat[key][0], labels=to_cat[key][1])
            else:
                self._survey_to_cat_single(
                    key, to_cat[key])

    def _set_survey_from_frame(self, frame_survey,
                              inverse = False, drop = False, to_cat = False):
        """define survey from DataFrame"""
        if inverse:
            self.inverse = inverse
        self.survey = frame_survey
        self._cut_survey(drop = drop)
        if to_cat:
            self._add_cat(to_cat)

    def _set_tot_population(self, total_pop):
        """Add total population column to census"""
        if not total_pop and self.pop_col in self.census.columns:
            print("Warning: using total population column on file --> ", self.pop_col)
            pass
        if not total_pop and self.pop_col not in self.census.columns:
            raise ValueError('need total population')
        elif total_pop and self.pop_col in self.census.columns:
            print("Warning: will overwrite total population column on census")
            self.census.loc[:, self.pop_col] = total_pop
        elif total_pop and self.pop_col not in self.census.columns:
            self.census.loc[:, self.pop_col] = total_pop

    def _set_census_from_file(self, file_census, total_pop=False, **kwargs):
        """define census from file"""
        self.census = pd.read_csv(file_census, **kwargs)
        self._set_tot_population(total_pop)

    def _set_census_from_frame(self, frame_census, total_pop=False):
        """define census from DataFrame"""
        self.census = frame_census
        self._set_tot_population(total_pop)


#TODO Internalized into PopModel class.
def _plot_single_error(survey_var, census_key, survey, census, pop,
                       weight = 'wf',
                       save_all = False, year = 2010, raw = False):
    """Plot error distrinution for single variable"""
    Rec_s = survey.loc[:, [survey_var, weight]].groupby(survey_var).sum()
    Rec_c = census.loc[[year], [c for c in census.columns if census_key in c]]
    Rec = Rec_c.T.join(Rec_s)
    if raw:
        return(Rec)
    Rec_0 = Rec.loc[Rec.wf.isnull()]
    diff = (Rec.loc[:, weight] - Rec.loc[:, year]).div(pop).mul(100)
    diff_0 = (Rec_0.loc[:, year]).div(pop).mul(-100)
    if save_all:
        ax = diff.plot(kind='bar')
        diff.plot(kind='bar', ax=ax)
        ax.set_ylabel("Error [%]")
        plt.tight_layout()
        plt.savefig("FIGURES/error_{}.png".format(census_key), dpi=300)
        plt.cla()
    return(diff, diff_0)


def _get_plot_var(trace, census, skip_cols, fit_cols):
    """Get variables to plot"""
    plot_variables = dict()
    for c in trace.columns:
        if c not in skip_cols and c not in fit_cols:
            for cc in census.columns:
                for c_split in c.split('_')[1:]:
                    if c_split in cc or c_split.lower() in cc:
                        plot_variables[c] = cc.split('_')[0]
    return(plot_variables)


def plot_error(trace_in, census_in, iterations,
               pop = False,
               skip = list(),
               fit_col = list(),
               plot_name = False, wbins = 50,
               wspace = 0.2, hspace = 0.9, top = 0.91,
               year = 2010, save_all = False):
    """Plot modeling errro distribution"""
    if isinstance(trace_in, str) and os.path.isfile(trace_in):
        trace = pd.read_csv(trace_in, index_col = 0)
    elif isinstance(trace_in, pd.DataFrame):
        trace = trace_in
    else:
        raise TypeError('trace must either be a valid file on disc of a pandas DataFrame')

    if isinstance(census_in, str) and os.path.isfile(census_in):
        census = pd.read_csv(census_in, index_col = 0)
    elif isinstance(census_in, pd.DataFrame):
        census = census_in
    else:
        raise TypeError('census must either be a valid file on disc of a pandas DataFrame')

    skip_cols = ['w', 'wf', 'level_0', 'index']
    skip_cols.extend(skip)

    fit_cols = ['Income', 'Electricity','Water']
    for fc in fit_col:
        if fc not in fit_cols:
            fit.cols.append(fc)

    # Colors
    sn_blue = sns.color_palette()[0] #blue
    # sn_orange = sns.color_palette()[1] #orange
    # color = sns.color_palette()[2] #green
    sn_red = sns.color_palette()[3] #red
    # sn_grey = sns.color_palette()[5] #grey
    # color = sns.color_palette()[6] #pink
    sn_grey = sns.color_palette()[7] #grey
    # color = sns.color_palette()[8] #yellow
    # color = sns.color_palette()[9] #cyan
    #
    if not pop:
        pop = census.loc[year, 'pop']

    plot_variables = _get_plot_var(trace, census, skip_cols, fit_cols)
    Diff = list()
    Diff_0 = list()
    for pv in plot_variables:
        Rec, Rec_0 = _plot_single_error(pv, plot_variables[pv], trace, census, pop, save_all=save_all, year=year)
        Diff.append(Rec)
        Diff_0.append(Rec_0)
    Diff = pd.concat(Diff)
    Diff_0 = pd.concat(Diff_0)
    fit_error = list()
    fit_error_abs = list()
    for col in fit_cols:
        Rec_s = trace.loc[:, col].mul(trace.wf).sum()
        Rec_c = census.loc[year, col]
        Rec = (Rec_s - Rec_c) / Rec_c * 100
        fit_error.append(Rec)
        fit_error_abs.append(abs(Rec_s - Rec_c))

    fig, ((ax, ax1), (ax2, ax3)) = plt.subplots(2, 2, figsize=(15, 10))
    #TODO fix location of title
    fig.suptitle("Sampling error (year = {}, n = {})".format(year, iterations), fontsize="x-large")
    x = Diff.index.get_indexer_for(Diff_0.index)
    Diff.plot(kind='bar', label="PSAE", ax=ax, color =  sn_blue)
    if Diff_0.shape[0] >= 1:
        ax.bar(x, Diff_0, color=sn_grey, label='missing parameters', width=0.6)
        b_labels = ["{:0.2f}%".format(i) for i in Diff_0]

        for x, y, l in zip(x, Diff_0, b_labels):
            # height = rect.get_height()
            if y < 0:
                va_pos = 'top'
            else:
                va_pos = 'bottom'
            ax.text(x, y, l,
                    ha='center', va=va_pos, color='grey', alpha=0.7)

    ax.plot((0, Diff.shape[0]), (Diff[Diff >= 0].mean(), Diff[Diff >= 0].mean()),
            '--', color = sn_red, label='(+)PSAE = {:0.2f}% Overestimate'.format(Diff[Diff >= 0].mean()), alpha=0.4)
    ax.plot((0, Diff.shape[0]), (Diff[Diff < 0].mean(), Diff[Diff < 0].mean()),
            '--', color = sn_red, label='(-)PSAE = {:0.2f}% Underestimate'.format(Diff[Diff < 0].mean()), alpha=0.4)
    ax.set_ylabel("PSAE [%]")
    ax.set_title("Percentage Standardized Absolute Error (PSAE)")
    ax.legend()
    ax.ticklabel_format(axis='y', style='sci', scilimits=(-2,2))
    plt.tight_layout()
    x1 = [i for i in range(len(fit_cols))]
    ax1.bar(x1, fit_error, label='Error on fitted values', color = sn_blue)
    ax1.set_xticks(x1)
    ax1.set_xticklabels(fit_cols, rotation='90')
    ax1.set_title("Percentage Error of Estimated Variables")
    ax1.set_ylabel("Error [%]")
    rects = ax1.patches
    labels = ["{:0.2E}\n{:0.2E}%".format(i, j) for i,j in zip(fit_error_abs, fit_error)]
    for rect, label in zip(rects, labels):
        height = rect.get_height()
        if not np.isnan(height):
            ax1.text(rect.get_x() + rect.get_width()/2, height/2,
                 label, ha = 'center', va = 'top', color = sn_red)
    ax1.legend(loc=4)
    ax1.ticklabel_format(axis='y', style='sci', scilimits=(-2,2))
    rec = list()
    for pv in plot_variables:
        rec.append(
            _plot_single_error(pv, plot_variables[pv], trace, census, pop, raw=True, year=year))
    REC = pd.concat(rec)
    REC.columns = ['Observed', 'Simulated']
    TAE = abs((REC.Observed - REC.Simulated).mean())
    PSAE = TAE / pop * 100
    rec_corr = REC.corr().iloc[0, 1]
    ## X = add_constant(REC.Simulated)
    ## results = OLS(REC.Observed, X).fit()
    ## N = results.nobs
    ## P = results.df_model
    ## dfn, dfd = P, N - P - 1
    ## F = results.mse_model / results.mse_resid
    ## p = 1.0 - stats.f.cdf(F,dfn,dfd)
    value_text = """
PearsonR: {:0.2f}

TAE: {:0.2E}, PSAE: {:0.2E}%
    """.format(rec_corr, TAE, PSAE)
    sns.regplot(y = "Observed", x = "Simulated", data = REC, ax = ax2, color = sn_blue)
    ax2.text(0, REC.Observed.max(),
             value_text,
             color = sn_red,
             va = 'top')
    ax2.set_title("Simulated and Observed marginal sums")
    ax2.ticklabel_format(axis='y', style='sci', scilimits=(-2,2))
    ax2.ticklabel_format(axis='x', style='sci', scilimits=(-2,2))

    val, bins = np.histogram(trace.wf, bins = wbins)
    sns.distplot(trace.wf,
                 bins = bins, kde = False,
                 label = 'Distribution of estimated new weights',
                 color = sn_blue,
                 hist_kws={"alpha": 1},
                 ax = ax3)
    ax3.set_xlabel('Estimated weights')
    ax3.plot((trace.w.mean(), trace.w.mean()),
             (0, val.max()), '--', color = sn_red, alpha = 1,
             label='Original sample weight')
    ax3.set_ylim(0, val.max())
    ax3.set_xlim(trace.wf.max()/100*-1, trace.wf.max())
    ax3.text(trace.w.mean(), val.max()/2,
             " <-- $d = {:0.2f}$".format(trace.w.mean()),
             color = sn_red,
             va = 'top')
    ax3.legend()
    ax3.set_title("Distribution of estimated new sample weights")
    ax3.ticklabel_format(axis='y', style='sci', scilimits=(-2,2))

    fig.subplots_adjust(wspace = wspace)
    fig.subplots_adjust(hspace = hspace)
    fig.subplots_adjust(top=top)
    # plt.tight_layout()

    if not plot_name:
        plot_name = 'sampling_error_{}_{}'.format(iterations, year)

    plt.savefig("FIGURES/{}.png".format(plot_name), dpi=300)

    return(REC)
