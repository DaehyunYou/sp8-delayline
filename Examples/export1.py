#!/usr/bin/env python3


# %% import
from argparse import ArgumentParser
from functools import reduce
from glob import iglob
from itertools import chain, islice
from math import sin, cos
from typing import List

from pyspark.sql import SparkSession, DataFrame
from pyspark.sql.functions import udf, array, size

from dltools import (in_degree, in_milli_meter, in_electron_volt, in_gauss, Hit, SpkHits,
                     uniform_electric_field, none_field, electron_spectrometer)

# %% parser & parameters
parser = ArgumentParser(prog='sp8export', description="""\
Export SP8 analyzed data.""")
parser.add_argument('rootfiles', metavar='filename', type=str, nargs='+',
                    help='resorted ROOT files for the analysis')
parser.add_argument('-o', '--output', metavar='filename', type=str, default='exported.parquet',
                    help='filename where to export analyzed data')
args = parser.parse_args()
targetfiles = args.rootfiles
save_as = args.output


# %% initialize spark builder
builder = (SparkSession
           .builder
           .appName(parser.prog)
           .config("spark.jars.packages", "org.diana-hep:spark-root_2.11:0.1.15")
           .config("spark.cores.max", 11)
           .config("spark.executor.cores", 5)
           .config("spark.executor.memory", "4g")
           )


# %% initialize spectrometers
#
#   ion2nd                 ion1st       electron
# ┌───┐│                      │             │                    │┌───┐
# │   ││                      │             │                    ││   │
# │   ││                      │             │                    ││   │
# │   ││                      │             │                    ││   │
# │ion││                      │             │                    ││ele│
# │mcp││        acc_reg       │   sep_reg   │     draft_reg      ││mcp│
# │   ││                      │             │                    ││   │
# │   ││                      │             │                    ││   │
# │   ││                      │────x────────│                    ││   │
# │   ││                      │             │                    ││   │
# └───┘│                      │             │                    │└───┘
#
#                        uniform magnetic field
#                       symbol x: reaction point
#
c = {
    'draft_reg': 67.4,  # mm
    'elesep_reg': 33,  # mm
    'ionsep_reg': 16.5,  # mm
    'acc_reg': 82.5,  # mm
    'mcpgep_reg': 10,  # mm
    'electron_epoten': -200,  # V
    'ion1st_epoten': -285,  # V
    'ion2nd_epoten': -2000,  # V
    'ionmcp_epoten': -3200,  # V
    'uniform_mfield': 6.843,  # Gauss
}
ion_acc = (
        uniform_electric_field(length=in_milli_meter(c['mcpgep_reg']),
                               electric_field=(in_electron_volt(c['ion2nd_epoten'] - c['ionmcp_epoten'])
                                               / in_milli_meter(c['mcpgep_reg']))) *
        uniform_electric_field(length=in_milli_meter(c['acc_reg']),
                               electric_field=(in_electron_volt(c['ion1st_epoten'] - c['ion2nd_epoten'])
                                               / in_milli_meter(c['acc_reg']))) *
        uniform_electric_field(length=in_milli_meter(c['ionsep_reg']),
                               electric_field=(in_electron_volt(c['electron_epoten'] - c['ion1st_epoten'])
                                               / in_milli_meter(c['ionsep_reg'] + c['elesep_reg'])))
)
ele_acc = (
        none_field(length=in_milli_meter(c['draft_reg'])) *
        uniform_electric_field(length=in_milli_meter(c['elesep_reg']),
                               electric_field=(in_electron_volt(c['ion1st_epoten'] - c['electron_epoten'])
                                               / in_milli_meter(c['ionsep_reg'] + c['elesep_reg'])))
)
ele_spt = electron_spectrometer(ele_acc, magnetic_filed=in_gauss(c['uniform_mfield']))


# %% spark udfs
@udf(SpkHits)
def combine_hits(tarr: List[float],
                 xarr: List[float],
                 yarr: List[float],
                 flagarr: List[float],  # todo: nullable
                 nhits: int,  # todo: nullable
                 ) -> List[dict]:
    zipped = ({'t': t, 'x': x, 'y': y, 'flag': f} for t, x, y, f in zip(tarr, xarr, yarr, flagarr))
    return list(islice(zipped, nhits))


@udf(SpkHits)
def analyse_ihits(hits: List[dict]) -> List[dict]:
    hasgoodflag = ({'t': h['t'] - -101.590,
                    'x': 1.12 * (h['x'] - 0.7719),
                    'y': 1.12 * (h['y'] - -1.99585),
                    } for h in hits if not 14 < h['flag'])
    notdead = [h for h in hasgoodflag if 0 < h['t'] < 18000]
    if len(notdead) < 3:
        return []
    if ((3500 < notdead[0]['t'] < 9750) and
        (3500 < notdead[1]['t'] < 9750) and
        (3500 < notdead[2]['t'] < 9750)):
        return notdead
    return []


@udf(SpkHits)
def analyse_ehits(hits: List[dict]) -> List[dict]:
    th = in_degree(-30)
    hasgoodflag = ({'t': h['t'] - -160.322,
                    'x': 1.03840 * (cos(th)*h['x'] - sin(th)*h['y'] - 0),
                    'y': 1.05967 * (sin(th)*h['x'] + cos(th)*h['y'] - 0.082456),
                    'as': {},
                    } for h in hits if not 14 < h['flag'])
    notdead = [h for h in hasgoodflag if 0 < h['t'] < 60]
    [h['as'].update(e=ele_spt(Hit.in_experimental_units(t=h['t'], x=h['x'], y=h['y']))
                      .to_experimental_units())
     for h in notdead if 20 < h['t'] < 40]
    return notdead


# %% connect to spark master & read root files
with builder.getOrCreate() as spark:
    globbed = (iglob(f) for f in targetfiles)
    filenames = sorted(set(chain.from_iterable(globbed)))
    loaded = (spark.read.format("org.dianahep.sparkroot").load(f) for f in filenames)
    df = reduce(DataFrame.union, loaded)

    # %% restructure data
    imhits = sum(1 for c in df.columns if c.startswith('IonT'))
    emhits = sum(1 for c in df.columns if c.startswith('ElecT'))
    restructured = (
        df
            .withColumn('itarr', array(*['IonT{}'.format(i) for i in range(imhits)]))
            .withColumn('ixarr', array(*['IonX{}'.format(i) for i in range(imhits)]))
            .withColumn('iyarr', array(*['IonY{}'.format(i) for i in range(imhits)]))
            .withColumn('iflagarr', array(*['IonFlag{}'.format(i) for i in range(imhits)]))
            .withColumnRenamed('IonNum', 'inhits')
            .withColumn('etarr', array(*['ElecT{}'.format(i) for i in range(emhits)]))
            .withColumn('exarr', array(*['ElecX{}'.format(i) for i in range(emhits)]))
            .withColumn('eyarr', array(*['ElecY{}'.format(i) for i in range(emhits)]))
            .withColumn('eflagarr', array(*['ElecFlag{}'.format(i) for i in range(emhits)]))
            .withColumnRenamed('ElecNum', 'enhits')
            .select(combine_hits('itarr', 'ixarr', 'iyarr', 'iflagarr', 'inhits').alias('ihits'),
                    combine_hits('etarr', 'exarr', 'eyarr', 'eflagarr', 'enhits').alias('ehits'))
    )

    # %% analyse hits & export them
    analyzed = (restructured
                .withColumn('ihits', analyse_ihits('ihits'))
                .filter(0 < size('ihits'))
                .withColumn('ehits', analyse_ehits('ehits'))
                .filter(0 < size('ehits'))
                )
    (analyzed
     .write
     .option("maxRecordsPerFile", 10000)  # less than 10 MB assuming a record of 1 KB,
     .parquet(save_as)
     )
