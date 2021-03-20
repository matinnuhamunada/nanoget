"""
This module provides functions to extract useful metrics
from Oxford Nanopore sequencing reads and alignments.


Data can be presented in the following formats, using the following functions:
- A sorted bam file
  process_bam(bamfile, threads)
- A standard fastq file
  process_fastq_plain(fastqfile, 'threads')
- A fastq file with metadata from MinKNOW or Albacore
  process_fastq_rich(fastqfile)
- A sequencing_summary file generated by Albacore
  process_summary(sequencing_summary.txt, 'readtype')

Fastq files can be compressed using gzip, bzip2 or bgzip.
The data is returned as a pandas DataFrame with standardized headernames for convenient extraction.
The functions perform logging while being called and extracting data.
"""

import sys
import logging
import pandas as pd
from functools import partial
import concurrent.futures as cfutures
import nanoget.extraction_functions as ex


def get_input(source, files, threads=4, readtype="1D",
              combine="simple", names=None, barcoded=False, huge=False, keep_supp=True):
    """Get input and process accordingly.

    Data can be:
    - a uncompressed, bgzip, bzip2 or gzip compressed fastq file
    - a uncompressed, bgzip, bzip2 or gzip compressed fasta file
    - a rich fastq containing additional key=value information in the description,
      as produced by MinKNOW and albacore with the same compression options as above
    - a sorted bam file
    - a sorted cram file
    - a (compressed) sequencing_summary.txt file generated by albacore/guppy

    If data is huge=True then no parallelization (using concurrent.futures) is used,
     as pickling these dataframes leads to crashes

    Handle is passed to the proper functions to get DataFrame with metrics
    Multiple files of the same type can be used to extract info from, which is done in parallel
    Arguments:
    - source: defines the input data type and the function that needs to be called
    - files: is a list of one or more files to operate on, from the type of <source>
    - threads: is the amount of workers which can be used
    - readtype: (only relevant for summary input) and specifies which columns have to be extracted
    - combine: is either 'simple' or 'track', with the difference that  with 'track' an additional
      field is created with the name of the dataset
    - names: if combine="track", the names to be used for the datasets. Needs to have same length as
      files, or None
    """
    proc_functions = {
        'fastq': ex.process_fastq_plain,
        'fasta': ex.process_fasta,
        'bam': ex.process_bam,
        'summary': ex.process_summary,
        'fastq_rich': ex.process_fastq_rich,
        'fastq_minimal': ex.process_fastq_minimal,
        'cram': ex.process_cram,
        'ubam': ex.process_ubam, }

    if source not in proc_functions.keys():
        logging.error("nanoget: Unsupported data source: {}".format(source))
        sys.exit("Unsupported data source: {}".format(source))
    filethreads = min(len(files), threads)
    threadsleft = threads - filethreads or 1
    if huge:
        logging.info("nanoget: Running with a single huge input file.")
        if not len(files) == 1:
            logging.error("nanoget: Using multiple huge input files is currently not supported.")
            sys.exit("Using multiple huge input files is currently not supported.\n"
                     "Please let me know on GitHub if that's of interest for your application.\n")

        datadf = proc_functions[source](files[0],
                                        threads=threadsleft,
                                        readtype=readtype,
                                        barcoded=barcoded,
                                        keep_supp=keep_supp,
                                        huge=True)
    else:
        with cfutures.ProcessPoolExecutor(max_workers=filethreads) as executor:
            extraction_function = partial(proc_functions[source],
                                          threads=threadsleft,
                                          readtype=readtype,
                                          barcoded=barcoded,
                                          keep_supp=keep_supp,
                                          huge=False)
            datadf = combine_dfs(
                dfs=[out for out in executor.map(extraction_function, files)],
                names=names or files,
                method=combine)
    if "readIDs" in datadf.columns and pd.isna(datadf["readIDs"]).any():
        datadf.drop("readIDs", axis='columns', inplace=True)
    datadf = calculate_start_time(datadf)
    logging.info("Nanoget: Gathered all metrics of {} reads".format(len(datadf)))
    if len(datadf) == 0:
        logging.critical("Nanoget: no reads retrieved.")
        sys.exit("Fatal: No reads found in input.")
    else:
        return datadf


def combine_dfs(dfs, names=None, method='simple'):
    """Combine dataframes.

    Combination is either done simple by just concatenating the DataFrames
    or performs tracking by adding the name of the dataset as a column."""
    if method == "track":
        return pd.concat([df.assign(dataset=n) for df, n in zip(dfs, names)],
                         ignore_index=True)
    elif method == "simple":
        return pd.concat(dfs, ignore_index=True)


def calculate_start_time(df):
    """Calculate the start_time per read.

    Time data is either
    a "time" (in seconds, derived from summary files) or
    a "timestamp" (in UTC, derived from fastq_rich format)
    and has to be converted appropriately in a datetime format time_arr

    For both the time_zero is the minimal value of the time_arr,
    which is then used to subtract from all other times

    In the case of method=track (and dataset is a column in the df) then this
    subtraction is done per dataset
    """
    if "time" in df.columns:
        df["time_arr"] = pd.Series(df["time"], dtype='datetime64[s]')
    elif "timestamp" in df.columns:
        df["time_arr"] = pd.Series(df["timestamp"], dtype="datetime64[ns]")
    else:
        return df
    if "dataset" in df.columns:
        for dset in df["dataset"].unique():
            time_zero = df.loc[df["dataset"] == dset, "time_arr"].min()
            df.loc[df["dataset"] == dset, "start_time"] = \
                df.loc[df["dataset"] == dset, "time_arr"] - time_zero
    else:
        df["start_time"] = df["time_arr"] - df["time_arr"].min()
    return df.drop(["time", "timestamp", "time_arr"], axis=1, errors="ignore")
