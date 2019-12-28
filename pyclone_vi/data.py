from collections import OrderedDict

import numba
import numpy as np
import pandas as pd

from pyclone_vi.math_utils import log_beta_binomial_pdf, log_binomial_pdf, log_normalize, log_sum_exp


def load_data(file_name, density='binomial', num_grid_points=100, precision=200):
    data, mutations, samples = load_pyclone_data(file_name)

    log_p_data = []

    for data_point in data.values():
        log_p_data.append(
            data_point.to_likelihood_grid(density, num_grid_points, precision=precision)
        )

    return np.stack(log_p_data), mutations, samples


def load_pyclone_data(file_name):
    df = pd.read_csv(file_name, sep='\t')

    df['sample_id'] = df['sample_id'].astype(str)

    samples = sorted(df['sample_id'].unique())

    # Filter for mutations present in all samples
    df = df.groupby(by='mutation_id').filter(lambda x: sorted(x['sample_id'].unique()) == samples)

    mutations = sorted(df['mutation_id'].unique())

    print('Samples: {}'.format(' '.join(samples)))

    print('Num mutations: {}'.format(len(mutations)))

    if 'error_rate' not in df.columns:
        df.loc[:, 'error_rate'] = 1e-3

    if 'tumour_content' not in df.columns:
        print('Tumour content column not found. Setting values to 1.0.')

        df.loc[:, 'tumour_content'] = 1.0

    print()

    data = OrderedDict()

    for name in mutations:
        mut_df = df[df['mutation_id'] == name]

        sample_data_points = []

        mut_df = mut_df.set_index('sample_id')

        for sample in samples:
            row = mut_df.loc[sample]

            a = row['ref_counts']

            b = row['alt_counts']

            cn, mu, log_pi = get_major_cn_prior(
                row['major_cn'],
                row['minor_cn'],
                row['normal_cn'],
                error_rate=row['error_rate']
            )

            sample_data_points.append(
                SampleDataPoint(a, b, cn, mu, log_pi, row['tumour_content'])
            )

        data[name] = DataPoint(samples, sample_data_points)

    return data, mutations, samples


def get_major_cn_prior(major_cn, minor_cn, normal_cn, error_rate=1e-3):
    total_cn = major_cn + minor_cn

    cn = []

    mu = []

    log_pi = []

    # Consider all possible mutational genotypes consistent with mutation before CN change
    for x in range(1, major_cn + 1):
        cn.append((normal_cn, normal_cn, total_cn))

        mu.append((error_rate, error_rate, min(1 - error_rate, x / total_cn)))

        log_pi.append(0)

    # Consider mutational genotype of mutation before CN change if not already added
    mutation_after_cn = (normal_cn, total_cn, total_cn)

    if mutation_after_cn not in cn:
        cn.append(mutation_after_cn)

        mu.append((error_rate, error_rate, min(1 - error_rate, 1 / total_cn)))

        log_pi.append(0)

        assert len(set(cn)) == 2

    cn = np.array(cn, dtype=int)

    mu = np.array(mu, dtype=float)

    log_pi = log_normalize(np.array(log_pi, dtype=float))

    return cn, mu, log_pi


class DataPoint(object):
    def __init__(self, samples, sample_data_points):
        self.samples = samples

        self.sample_data_points = sample_data_points

    def get_ccf_grid(self, grid_size, eps=1e-6):
        return np.linspace(eps, 1 - eps, grid_size)

    def to_dict(self):
        return OrderedDict(zip(self.samples, self.sample_data_points))

    def to_likelihood_grid(self, density, num_grid_points, precision=200):
        shape = (len(self.samples), num_grid_points)

        log_ll = np.zeros(shape)

        for s_idx, data_point in enumerate(self.sample_data_points):
            for i, ccf in enumerate(self.get_ccf_grid(num_grid_points)):
                if density == 'beta-binomial':
                    log_ll[s_idx, i] = log_pyclone_beta_binomial_pdf(data_point, ccf, precision)

                elif density == 'binomial':
                    log_ll[s_idx, i] = log_pyclone_binomial_pdf(data_point, ccf)

        return log_ll


@numba.jitclass([
    ('a', numba.int64),
    ('b', numba.int64),
    ('cn', numba.int64[:, :]),
    ('mu', numba.float64[:, :]),
    ('log_pi', numba.float64[:]),
    ('t', numba.float64)
])
class SampleDataPoint(object):
    def __init__(self, a, b, cn, mu, log_pi, t):
        self.a = a
        self.b = b
        self.cn = cn
        self.mu = mu
        self.log_pi = log_pi
        self.t = t


@numba.jit(nopython=True)
def log_pyclone_beta_binomial_pdf(data, f, s):
    t = data.t

    C = len(data.cn)

    population_prior = np.zeros(3)
    population_prior[0] = (1 - t)
    population_prior[1] = t * (1 - f)
    population_prior[2] = t * f

    ll = np.ones(C, dtype=np.float64) * np.inf * -1

    for c in range(C):
        e_vaf = 0

        norm_const = 0

        for i in range(3):
            e_cn = population_prior[i] * data.cn[c, i]

            e_vaf += e_cn * data.mu[c, i]

            norm_const += e_cn

        e_vaf /= norm_const

        a = e_vaf * s

        b = s - a

        ll[c] = data.log_pi[c] + log_beta_binomial_pdf(data.a + data.b, data.b, a, b)

    return log_sum_exp(ll)


@numba.jit(nopython=True)
def log_pyclone_binomial_pdf(data, f):
    t = data.t

    C = len(data.cn)

    population_prior = np.zeros(3)
    population_prior[0] = (1 - t)
    population_prior[1] = t * (1 - f)
    population_prior[2] = t * f

    ll = np.ones(C, dtype=np.float64) * np.inf * -1

    for c in range(C):
        e_vaf = 0

        norm_const = 0

        for i in range(3):
            e_cn = population_prior[i] * data.cn[c, i]

            e_vaf += e_cn * data.mu[c, i]

            norm_const += e_cn

        e_vaf /= norm_const

        ll[c] = data.log_pi[c] + log_binomial_pdf(data.a + data.b, data.b, e_vaf)

    return log_sum_exp(ll)