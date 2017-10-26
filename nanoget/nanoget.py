# wdecoster
'''
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
'''

from __future__ import division
import sys
import os
import logging
import re
import pandas as pd
import numpy as np
from functools import partial
from Bio import SeqIO
import concurrent.futures as cfutures
import pysam
import nanomath


def get_input(source, files, threads=4, readtype="1D", combine="simple", names=None):
    '''
    Get input and process accordingly.
    Data can be:
    - a uncompressed, bgzip, bzip2 or gzip compressed fastq file
    - a rich fastq containing additional key=value information in the description,
      as produced by MinKNOW and albacore
    - a sorted bam file
    - a sequencing_summary.txt file generated by albacore

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
    '''
    proc_functions = {
        'fastq': process_fastq_plain,
        'bam': process_bam,
        'summary': process_summary,
        'fastq_rich': process_fastq_rich,
        'fastq_minimal': process_fastq_minimal}
    filethreads = min(len(files), threads)
    threadsleft = threads - filethreads
    if threadsleft > len(files):
        pass
    with cfutures.ProcessPoolExecutor(max_workers=filethreads) as executor:
        datadf = combine_dfs(
            dfs=[out for out in executor.map(
                partial(proc_functions[source], threads=threadsleft, readtype=readtype), files)],
            names=names or files,
            method=combine)
    if "time" in datadf:
        a_time_stamps = np.array(datadf["time"], dtype='datetime64[s]')
        datadf["start_time"] = a_time_stamps - np.amin(a_time_stamps)
        datadf.drop("time", axis=1, inplace=True)
    if "timestamp" in datadf:
        datadf["time_arr"] = pd.Series(datadf["timestamp"], dtype="datetime64[ns]")
        datadf["start_time"] = datadf["time_arr"] - datadf["time_arr"].min()
        datadf.drop(["timestamp", "time_arr"], axis=1, inplace=True)
    logging.info("Nanoget: Gathered all metrics")
    return datadf


def combine_dfs(dfs, names, method):
    if method == "track":
        res = []
        for df, identifier in zip(dfs, names):
            df["dataset"] = identifier
            res.append(df)
        return pd.concat(res, ignore_index=True)
    elif method == "simple":
        return pd.concat(dfs, ignore_index=True)


def check_existance(f):
    '''
    Check if the file supplied as input exists
    '''
    if not os.path.isfile(f):
        logging.error("Nanoget: File provided doesn't exist or the path is incorrect: {}".format(f))
        sys.exit("File provided doesn't exist or the path is incorrect: {}".format(f))


def process_summary(summaryfile, threads, readtype):
    '''
    Extracting information from an albacore summary file.
    Only reads which have a >0 length are returned.

    The fields below may or may not exist, depending on the type of sequencing performed.
    Fields 1-14 are for 1D sequencing.
    Fields 1-23 for 2D sequencing.
    Fields 24-27, 2-5, 22-23 for 1D^2 (1D2) sequencing
     1  filename
     2  read_id
     3  run_id
     4  channel
     5  start_time
     6  duration
     7  num_events
     8  template_start
     9  num_events_template
    10  template_duration
    11  num_called_templatexticks
    12  sequence_length_template
    13  mean_qscore_template
    14  strand_score_template
    15  complement_start
    16    num_events_complement
    17    complement_duration
    18    num_called_complement
    19    sequence_length_complement
    20    mean_qscore_complement
    21    strand_score_complement
    22    sequence_length_2d
    23    mean_qscore_2d
    24    filename1
    25    filename2
    26    read_id1
    27    read_id2
    '''
    logging.info("Nanoget: Staring to collect statistics from summary file.")
    check_existance(summaryfile)
    logging.info("Collecting statistics for {} sequencing".format(readtype))
    if readtype == "1D":
        cols = ["read_id", "run_id", "channel", "start_time",
                "sequence_length_template", "mean_qscore_template"]
    elif readtype in ["2D", "1D2"]:
        cols = ["read_id", "run_id", "channel", "start_time",
                "sequence_length_2d", "mean_qscore_2d"]
    try:
        datadf = pd.read_csv(
            filepath_or_buffer=summaryfile,
            sep="\t",
            usecols=cols,
        )
    except ValueError:
        logging.error(
            "Nanoget: did not find expected columns in summary file:\n {}".format(', '.join(cols)))
        sys.exit("ERROR: expected columns in summary file not found:\n {}".format(', '.join(cols)))
    datadf.columns = ["readIDs", "runIDs", "channelIDs", "time", "lengths", "quals"]
    logging.info("Nanoget: Finished collecting statistics from summary file.")
    return datadf[datadf["lengths"] != 0]


def check_bam(bam):
    '''
    Check if bam file
    - exists
    - has an index (create if necessary)
    - is sorted by coordinate
    - has at least one mapped read
    '''
    check_existance(bam)
    samfile = pysam.AlignmentFile(bam, "rb")
    if not samfile.has_index():
        pysam.index(bam)
        samfile = pysam.AlignmentFile(bam, "rb")  # Need to reload the samfile after creating index
        logging.info("Nanoget: No index for bam file could be found, created index.")
    if not samfile.header['HD']['SO'] == 'coordinate':
        logging.error("Nanoget: Bam file not sorted by coordinate!.")
        sys.exit("Please use a bam file sorted by coordinate.")
    logging.info("Nanoget: Bam file contains {} mapped and {} unmapped reads.".format(
        samfile.mapped, samfile.unmapped))
    if samfile.mapped == 0:
        logging.error("Nanoget: Bam file does not contain aligned reads.")
        sys.exit("FATAL: not a single read was mapped in the bam file.")
    return samfile


def process_bam(bam, threads, readtype):
    '''
    Processing function: calls pool of worker functions
    to extract from a bam file the following metrics:
    -lengths
    -aligned lengths
    -qualities
    -aligned qualities
    -mapping qualities
    -edit distances to the reference genome scaled by read length
    Returned in a pandas DataFrame
    '''
    logging.info("Nanoget: Starting to collect statistics from bam file.")
    samfile = check_bam(bam)
    chromosomes = samfile.references
    params = zip([bam] * len(chromosomes), chromosomes)
    with cfutures.ProcessPoolExecutor() as executor:
        output = [results for results in executor.map(extract_from_bam, params)]
    # 'output' contains a tuple per worker, each tuple contains lists per metric
    # Unpacked by following nested list comprehensions
    datadf = pd.DataFrame(data={
        "lengths": np.array([x for y in [elem[0] for elem in output] for x in y]),
        "aligned_lengths": np.array([x for y in [elem[1] for elem in output] for x in y]),
        "quals": np.array([x for y in [elem[2] for elem in output] for x in y]),
        "aligned_quals": np.array([x for y in [elem[3] for elem in output] for x in y]),
        "mapQ": np.array([x for y in [elem[4] for elem in output] for x in y]),
        "percentIdentity": np.array([x for y in [elem[5] for elem in output] for x in y]),
    })
    logging.info("Nanoget: bam contains {} primary alignments.".format(datadf["lengths"].size))
    logging.info("Nanoget: Finished collecting statistics from bam file.")
    return datadf


def extract_from_bam(params):
    '''
    Worker function per chromosome
    loop over a bam file and create tuple with lists containing metrics:
    -lengths
    -aligned lengths
    -qualities
    -aligned qualities
    -mapping qualities
    -edit distances to the reference genome scaled by read length
    '''
    bam, chromosome = params
    samfile = pysam.AlignmentFile(bam, "rb")
    lengths = []
    alignedLengths = []
    quals = []
    alignedQuals = []
    mapQ = []
    pID = []
    for read in samfile.fetch(reference=chromosome, multiple_iterators=True):
        if not read.is_secondary:
            quals.append(nanomath.aveQual(read.query_qualities))
            alignedQuals.append(nanomath.aveQual(read.query_alignment_qualities))
            lengths.append(read.query_length)
            alignedLengths.append(read.query_alignment_length)
            mapQ.append(read.mapping_quality)
            pID.append(get_pID(read))
    return (lengths, alignedLengths, quals, alignedQuals, mapQ, pID)


def get_pID(read):
    '''
    Return the percent identity of a read
    based on the NM tag if present,
    if not calculate from MD tag and CIGAR string
    '''
    try:
        return 100 * (1 - read.get_tag("NM") / read.query_alignment_length)
    except KeyError:
        return 100 * (1 - (parse_MD(read.get_tag("MD")) + parse_CIGAR(read.cigartuples)) /
                      read.query_alignment_length)


def parse_MD(MDlist):
    '''
    Parse MD string to get number of mismatches and deletions
    '''
    return sum([len(item) for item in re.split('[0-9^]', MDlist)])


def parse_CIGAR(cigartuples):
    '''
    Count the insertions in the read using the CIGAR string
    '''
    return sum([item[1] for item in cigartuples if item[0] == 1])


def handle_compressed_fastq(inputfq):
    '''
    Check for which fastq input is presented and open a handle accordingly
    Can read from stdin, compressed files (gz, bz2, bgz) or uncompressed
    Relies on file extensions to recognize compression
    '''
    if inputfq == 'stdin':
        logging.info("Nanoget: Reading from stdin.")
        return sys.stdin
    else:
        check_existance(inputfq)
        if inputfq.endswith('.gz'):
            import gzip
            logging.info("Nanoget: Decompressing gzipped fastq.")
            return gzip.open(inputfq, 'rt')
        elif inputfq.endswith('.bz2'):
            import bz2
            logging.info("Nanoget: Decompressing bz2 compressed fastq.")
            return bz2.BZ2File(inputfq, 'rt')
        elif inputfq.endswith(('.fastq', '.fq', '.bgz')):
            return open(inputfq, 'r')
        else:
            logging.error("INPUT ERROR: Unrecognized file extension")
            sys.exit('''INPUT ERROR: Unrecognized file extension\n,
                        supported formats for --fastq are .gz, .bz2, .bgz, .fastq and .fq''')


def process_fastq_plain(fastq, threads, readtype):
    '''
    Processing function
    Iterate over a fastq file and extract metrics
    Parallelized, although there is not much to gain with using many threads
    Saturation already starts at threads=3
    '''
    logging.info("Nanoget: Starting to collect statistics from plain fastq file.")
    inputfastq = handle_compressed_fastq(fastq)
    with cfutures.ProcessPoolExecutor() as executor:
        output = [results for results in executor.map(
            extract_from_fastq, SeqIO.parse(inputfastq, "fastq")) if results is not None]
    logging.info("Nanoget: Finished collecting statistics from plain fastq file.")
    return pd.DataFrame(data={
        "lengths": np.array([item[1] for item in output]),
        "quals": np.array([item[0] for item in output])
    })


def extract_from_fastq(rec):
    '''
    Worker function for extraction of metrics from a fastq record Seq object
    If length 0, nanomath.aveQual will throw a ZeroDivisionError
    Skipping the read is okay then.
    '''
    try:
        return (nanomath.aveQual(rec.letter_annotations["phred_quality"]), len(rec))
    except ZeroDivisionError:
        return None


def stream_fastq_full(fastq, threads):
    '''
    Extract from a fastq file:
    -readname
    -average and median quality
    -read_lenght
    '''
    logging.info("Nanoget: Starting to collect full metrics from plain fastq file.")
    inputfastq = handle_compressed_fastq(fastq)
    with cfutures.ProcessPoolExecutor(max_workers=threads) as executor:
        for results in executor.map(extract_all_from_fastq, SeqIO.parse(inputfastq, "fastq")):
            yield results
    logging.info("Nanoget: Finished collecting statistics from plain fastq file.")


def extract_all_from_fastq(rec):
    '''
    Worker function for extraction of metrics from a fastq record Seq object
    If length 0, nanomath.aveQual will throw a ZeroDivisionError
    Skipping the read is okay then.
    '''
    try:
        return (rec.id,
                len(rec),
                nanomath.ave_qual(rec.letter_annotations["phred_quality"]),
                nanomath.median_qual(rec.letter_annotations["phred_quality"]))
    except ZeroDivisionError:
        pass


def info_to_dict(info):
    '''
    Get the key-value pairs from the albacore/minknow fastq description and return as dictionary
    '''
    return {field.split('=')[0]: field.split('=')[1] for field in info.split(' ')[1:]}


def process_fastq_rich(fastq, threads, readtype):
    '''
    Extract information from fastq files generated by albacore or MinKNOW,
    containing richer information in the header (key-value pairs)
    read=<int> [72]
    ch=<int> [159]
    start_time=<timestamp> [2016-07-15T14:23:22Z]  # UTC ISO 8601 ISO 3339 timestamp
    Z indicates UTC time, T is the delimiter between date expression and time expression
    dateutil.parser.parse("2016-07-15T14:23:22Z") imported as dparse
    -> datetime.datetime(2016, 7, 15, 14, 23, 22, tzinfo=tzutc())
    '''
    logging.info("Nanoget: Starting to collect statistics from rich fastq file.")
    inputfastq = handle_compressed_fastq(fastq)
    lengths = []
    quals = []
    channels = []
    time_stamps = []
    runids = []
    for record in SeqIO.parse(inputfastq, "fastq"):
        try:
            quals.append(nanomath.aveQual(record.letter_annotations["phred_quality"]))
            lengths.append(len(record))
            read_info = info_to_dict(record.description)
            channels.append(read_info["ch"])
            time_stamps.append(read_info["start_time"])
            runids.append(read_info["runid"])
        except ZeroDivisionError:  # If length 0, nanomath.aveQual will throw a ZeroDivisionError
            pass
        except KeyError:
            logging.error("Nanoget: keyerror when processing record {}".format(record.description))
            sys.exit("Unexpected fastq identifier:\n{}\n\n \
            missing one or more of expected fields 'ch', 'start_time' or 'runid'".format(
                record.description))
    datadf = pd.DataFrame(data={
        "lengths": np.array(lengths),
        "quals": np.array(quals),
        "channelIDs": np.int16(channels),
        "runIDs": np.array(runids),
        "timestamp": np.array(time_stamps)
    })
    logging.info("Nanoget: Finished collecting statistics from rich fastq file.")
    return datadf


def readfq(fp):
    '''
    Generator function adapted from https://github.com/lh3/readfq
    '''
    last = None  # this is a buffer keeping the last unprocessed line
    while True:  # mimic closure; is it a bad idea?
        if not last:  # the first record or a record following a fastq
            for l in fp:  # search for the start of the next record
                if l[0] in '>@':  # fasta/q header line
                    last = l[:-1]  # save this line
                    break
        if not last:
            break
        name, seqs, last = last[1:].partition(" ")[0], [], None
        for l in fp:  # read the sequence
            if l[0] in '@+>':
                last = l[:-1]
                break
            seqs.append(l[:-1])
        if not last or last[0] != '+':  # this is a fasta record
            yield name, ''.join(seqs), None  # yield a fasta record
            if not last:
                break
        else:  # this is a fastq record
            seq, leng, seqs = ''.join(seqs), 0, []
            for l in fp:  # read the quality
                seqs.append(l[:-1])
                leng += len(l) - 1
                if leng >= len(seq):  # have read enough quality
                    last = None
                    yield name, seq, ''.join(seqs)  # yield a fastq record
                    break
            if last:  # reach EOF before reading enough quality
                yield name, seq, None  # yield a fasta record instead
                break


def fq_minimal(fq):
    '''
    Quickly parse a fasta/fastq file - but makes expectations on the file format
    There will be dragons if unexpected format is used
    Expects a fastq_rich format, but extracts less
    Returns
    - timestamp
    - length
    '''
    try:
        while True:
            time = next(fq)[1:].split(" ")[4][11:-1]
            length = len(next(fq))
            next(fq)
            next(fq)
            yield time, length
    except StopIteration:
        yield None


def process_fastq_minimal(fastq, threads, readtype):
    '''
    Swiftly extract minimal features from a rich fastq file:
    - length
    - time
    '''
    infastq = handle_compressed_fastq(fastq)
    df = pd.DataFrame(
        data=[rec for rec in fq_minimal(infastq) if rec],
        columns=["timestamp", "lengths"]
    )
    return df[["timestamp", "lengths"]]


# To ensure backwards compatilibity, for a while, keeping exposed function names duplicated:
processSummary = process_summary
processBam = process_bam
processFastqPlain = process_fastq_plain
processFastq_rich = process_fastq_rich
