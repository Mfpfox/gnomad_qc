import argparse
import json
import logging
import sys
from typing import Dict, List, Optional, Union
import uuid

import hail as hl

from gnomad.resources.grch38.reference_data import telomeres_and_centromeres, get_truth_ht
from gnomad.utils.file_utils import file_exists
from gnomad.utils.filtering import add_filters_expr
from gnomad.utils.slack import slack_notifications
from gnomad.variant_qc.pipeline import train_rf_model
from gnomad.variant_qc.random_forest import (
    apply_rf_model,
    load_model,
    median_impute_features,
    pretty_print_runs,
    save_model,
)
from gnomad_qc.v3.resources import (
    allele_data,
    fam_stats,
    freq,
    get_checkpoint_path,
    get_filters,
    get_info,
    get_rf,
    get_rf_annotated,
    qc_ac,
    rf_run_hash_path,
)
from gnomad_qc.slack_creds import slack_token
hl.stop()
logging.basicConfig(format="%(levelname)s (%(name)s %(lineno)s): %(message)s")
logger = logging.getLogger("variant_qc_random_forest")
logger.setLevel(logging.INFO)

LABEL_COL = "rf_label"
TRAIN_COL = "rf_train"
PREDICTION_COL = "rf_prediction"
INFO_FEATURES = [
    "AS_QD",
    "AS_ReadPosRankSum",
    "AS_MQRankSum",
    "AS_SOR",
    "AS_pab_max",
]
FEATURES = [
    "InbreedingCoeff",
    "variant_type",
    "allele_type",
    "n_alt_alleles",
    "was_mixed",
    "AS_QD",
    "AS_pab_max",
    "AS_MQRankSum",
    "AS_SOR",
    "AS_ReadPosRankSum",
]
TRUTH_DATA = ["hapmap", "omni", "mills", "kgp_phase1_hc"]
INBREEDING_COEFF_HARD_CUTOFF = -0.3


def create_rf_ht(
    impute_features_by_variant_type: bool = True,
    group: str = "raw",
    n_partitions: int = 5000,
    checkpoint_path: Optional[str] = None,
) -> hl.Table:
    """
    Creates a Table with all necessary annotations for the random forest model.
    Annotations that are added:
        Features for RF:
            - InbreedingCoeff
            - variant_type
            - allele_type
            - n_alt_alleles
            - was_mixed
            - has_star
            - AS_QD
            - AS_pab_max
            - AS_MQRankSum
            - AS_SOR
            - AS_ReadPosRankSum
        Training sites (bool):
            - transmitted_singleton
            - fail_hard_filters - (ht.QD < 2) | (ht.FS > 60) | (ht.MQ < 30)
    Numerical features are median-imputed. If impute_features_by_variant_type is set, imputation is done based on the
    median of the variant type.
    :param bool impute_features_by_variant_type: Whether to impute features median by variant type
    :param str group: Whether to use 'raw' or 'adj' genotypes
    :param int n_partitions: Number of partitions to use for final annotated table
    :param str checkpoint_path: Optional checkpoint path for the Table before median imputation and/or aggregate summary
    :return: Hail Table ready for RF
    :rtype: Table
    """

    def calc_SOR(SB):
        refFw = hl.float64(SB[0] + 1)
        refRv = hl.float64(SB[1] + 1)
        altFw = hl.float64(SB[2] + 1)
        altRv = hl.float64(SB[3] + 1)
        symmetricalRatio = ((refFw * altRv) / (altFw * refRv)) + ((altFw * refRv) / (refFw * altRv))
        refRatio = hl.min(refRv, refFw) / hl.max(refRv, refFw)
        altRatio = hl.min(altFw, altRv) / hl.max(altFw, altRv)
        SOR = (hl.log(symmetricalRatio) + hl.log(refRatio) - hl.log(altRatio))

        return SOR

    ht = get_info(split=True).ht()

    ht = ht.annotate(info=ht.info.annotate(AS_SOR=calc_SOR(ht.info.AS_SB_TABLE)))

    # TODO: We need to add SOR annotation calculation to info_ht calculation and possibly a new info_ht with bad quality samples removed?

    ht.describe()
    ht = ht.transmute(**ht.info)
    ht = ht.select(
       "lowqual", "AS_lowqual", "FS", "MQ", "QD", *INFO_FEATURES
    )
    ht.describe()
    # TODO: add back in if inbreeding has been calculated
    #inbreeding_ht = hl.read_table('gs://gnomad/release/3.0/ht/gnomad.genomes.r3.0.nested.no_subsets.sites.ht/') # TODO: change path to resources
    #inbreeding_ht = inbreeding_ht.annotate(
    #    InbreedingCoeff=hl.or_missing(
    #        ~hl.is_nan(inbreeding_ht.InbreedingCoeff), inbreeding_ht.InbreedingCoeff
    #    )
    #)
    trio_stats_ht = fam_stats.ht()
    trio_stats_ht = trio_stats_ht.select(
        f"n_transmitted_{group}", f"ac_children_{group}"
    )

    truth_data_ht = get_truth_ht()
    allele_data_ht = allele_data.ht()
    allele_counts_ht = qc_ac.ht()

    logger.info("Annotating Table with all columns from multiple annotation Tables")
    ht = ht.annotate(
        #**inbreeding_ht[ht.key], # TODO: add back in if there is inbreeding coeff
        **trio_stats_ht[ht.key],
        **truth_data_ht[ht.key],
        **allele_data_ht[ht.key].allele_data,
        **allele_counts_ht[ht.key],
    )
    # Filter to only variants found in high quality samples or controls
    ht = ht.filter(
        (ht[f"ac_qc_samples_{group}"] > 0)
    )  # TODO: change to AS_lowqual for v3.1 or leave as is to be more consistent with v3.0?
    ht = ht.select(
        "a_index",
        "was_split",
        *FEATURES,
        *TRUTH_DATA,
        **{
            "transmitted_singleton": (ht[f"n_transmitted_{group}"] == 1)
            & (ht[f"ac_qc_samples_{group}"] == 2),
            "fail_hard_filters": (ht.QD < 2) | (ht.FS > 60) | (ht.MQ < 30),
        },
        singleton=ht.ac_release_samples_raw == 1,
        ac_raw=ht.ac_qc_samples_raw,
        ac=ht.ac_release_samples_adj,
        ac_qc_samples_unrelated_raw=ht.ac_qc_samples_unrelated_raw
    )

    ht = ht.repartition(n_partitions, shuffle=False)
    if checkpoint_path:
        ht = ht.checkpoint(checkpoint_path, overwrite=True)

    if impute_features_by_variant_type:
        ht = median_impute_features(ht, {"variant_type": ht.variant_type})

    summary = ht.group_by("omni", "mills", "transmitted_singleton",).aggregate(
        n=hl.agg.count()
    )
    logger.info("Summary of truth data annotations:")
    summary.show(20)

    return ht


def train_rf(ht, args):
    features = FEATURES
    test_intervals = args.test_intervals
    if args.no_inbreeding_coeff:
        features.remove("InbreedingCoeff")

    if args.vqsr_training:
        vqsr_ht = get_filters(f"vqsr_{args.vqsr_type}", split=True).ht()
        ht = ht.annotate(
            vqsr_POSITIVE_TRAIN_SITE=vqsr_ht[ht.key].info.POSITIVE_TRAIN_SITE,
            vqsr_NEGATIVE_TRAIN_SITE=vqsr_ht[ht.key].info.NEGATIVE_TRAIN_SITE,
        )
        tp_expr = ht.vqsr_POSITIVE_TRAIN_SITE
        fp_expr = ht.vqsr_NEGATIVE_TRAIN_SITE
    else:
        fp_expr = ht.fail_hard_filters
        tp_expr = ht.omni | ht.mills
        if not args.no_transmitted_singletons:
            tp_expr = tp_expr | ht.transmitted_singleton

    if test_intervals:
        if isinstance(test_intervals, str):
            test_intervals = [test_intervals]
        test_intervals = [
            hl.parse_locus_interval(x, reference_genome="GRCh38")
            for x in test_intervals
        ]

    ht = ht.annotate(tp=tp_expr, fp=fp_expr)
    
    if args.filter_centromere_telomere:
        rf_ht = ht.filter(~hl.is_defined(telomeres_and_centromeres.ht()[ht.locus]))
    else:
        rf_ht = ht
        
    rf_ht, rf_model = train_rf_model(
        rf_ht,
        rf_features=features,
        tp_expr=rf_ht.tp,
        fp_expr=rf_ht.fp,
        fp_to_tp=args.fp_to_tp,
        num_trees=args.num_trees,
        max_depth=args.max_depth,
        test_expr=hl.literal(test_intervals).any(
            lambda interval: interval.contains(rf_ht.locus)
        ),
    )

    logger.info("Joining original RF Table with training information")
    ht = ht.join(rf_ht, how="left")

    return ht, rf_model


def get_rf_runs(rf_json_fp: str) -> Dict:
    """
    Loads RF run data from JSON file.
    :param rf_json_fp: File path to rf json file.
    :return: Dictionary containing the content of the JSON file, or an empty dictionary if the file wasn't found.
    """
    if file_exists(rf_json_fp):
        with hl.hadoop_open(rf_json_fp) as f:
            return json.load(f)
    else:
        logger.warning(
            f"File {rf_json_fp} could not be found. Returning empty RF run hash dict."
        )
        return {}


def get_run_data(
    transmitted_singletons: bool,
    adj: bool,
    vqsr_training: bool,
    filter_centromere_telomere: bool,
    test_intervals: List[str],
    features_importance: Dict[str, float],
    test_results: List[hl.tstruct],
) -> Dict:
    """
    Creates a Dict containing information about the RF input arguments and feature importance
    :param bool transmitted_singletons: True if transmitted singletons were used in training
    :param bool adj: True if training variants were filtered by adj
    :param bool vqsr_training: True if VQSR training examples were used for RF training
    :param List of str test_intervals: Intervals withheld from training to be used in testing
    :param Dict of float keyed by str features_importance: Feature importance returned by the RF
    :param List of struct test_results: Accuracy results from applying RF model to the test intervals
    :return: Dict of RF information
    """
    if vqsr_training:
        transmitted_singletons = None

    run_data = {
        "input_args": {
            "transmitted_singletons": transmitted_singletons,
            "adj": adj,
            "vqsr_training": vqsr_training,
            "filter_centromere_telomere": filter_centromere_telomere,
        },
        "features_importance": features_importance,
        "test_intervals": test_intervals,
    }

    if test_results is not None:
        tps = 0
        total = 0
        for row in test_results:
            values = list(row.values())
            # Note: values[0] is the TP/FP label and values[1] is the prediction
            if values[0] == values[1]:
                tps += values[2]
            total += values[2]
        run_data["test_results"] = [dict(x) for x in test_results]
        run_data["test_accuracy"] = tps / total

    return run_data


def main(args):
    hl.init(log="/variant_qc_random_forest.log")

    if args.list_rf_runs:
        logger.info(f"RF runs:")
        pretty_print_runs(get_rf_runs(rf_run_hash_path()))

    if args.annotate_for_rf:
        ht = create_rf_ht(
            impute_features_by_variant_type=not args.impute_features_no_variant_type,
            group="adj" if args.adj else "raw",
            n_partitions=args.n_partitions,
            checkpoint_path=get_checkpoint_path("rf_annotation"),
        )
        ht.write(
            get_rf_annotated(args.adj).path, overwrite=args.overwrite,
        )
        logger.info(f"Completed annotation wrangling for random forests model training")

    if args.train_rf:
        run_hash = str(uuid.uuid4())[:8]
        rf_runs = get_rf_runs(rf_run_hash_path())
        while run_hash in rf_runs:
            run_hash = str(uuid.uuid4())[:8]

        ht, rf_model = train_rf(get_rf_annotated(args.adj).ht(), args)
        ht = ht.checkpoint(
            get_rf(data="training", run_hash=run_hash).path, overwrite=args.overwrite,
        )

        logger.info("Adding run to RF run list")
        rf_runs[run_hash] = get_run_data(
            transmitted_singletons=not args.no_transmitted_singletons,
            adj=args.adj,
            vqsr_training=args.vqsr_training,
            filter_centromere_telomere=args.filter_centromere_telomere,
            test_intervals=args.test_intervals,
            features_importance=hl.eval(ht.features_importance),
            test_results=hl.eval(ht.test_results),
        )

        with hl.hadoop_open(rf_run_hash_path(), "w") as f:
            json.dump(rf_runs, f)

        logger.info("Saving RF model")
        save_model(
            rf_model, get_rf(data="model", run_hash=run_hash), overwrite=args.overwrite,
        )

    else:
        run_hash = args.run_hash

    if args.apply_rf:
        logger.info(f"Applying RF model {run_hash}...")
        rf_model = load_model(get_rf(data="model", run_hash=run_hash))
        ht = get_rf(data="training", run_hash=run_hash).ht()
        features = hl.eval(ht.features)
        ht = apply_rf_model(ht, rf_model, features, label=LABEL_COL)

        logger.info("Finished applying RF model")
        ht = ht.annotate_globals(rf_hash=run_hash)
        ht = ht.checkpoint(
            get_rf("rf_result", run_hash=run_hash).path, overwrite=args.overwrite,
        )

        ht_summary = ht.group_by(
            "tp", "fp", TRAIN_COL, LABEL_COL, PREDICTION_COL
        ).aggregate(n=hl.agg.count())
        ht_summary.show(n=20)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--slack_channel", help="Slack channel to post results and notifications to."
    )
    parser.add_argument(
        "--overwrite",
        help="Overwrite all data from this subset (default: False)",
        action="store_true",
    )
    parser.add_argument(
        "--run_hash",
        help="Run hash. Created by --train_rf and only needed for --apply_rf without running --train_rf",
        required=False,
    )

    actions = parser.add_argument_group("Actions")
    actions.add_argument(
        "--list_rf_runs",
        help="Lists all previous RF runs, along with their hash, parameters and testing results.",
        action="store_true",
    )
    actions.add_argument(
        "--annotate_for_rf",
        help="Creates an annotated ht with features for RF",
        action="store_true",
    )
    actions.add_argument("--train_rf", help="Trains RF model", action="store_true")
    actions.add_argument(
        "--apply_rf", help="Applies RF model to the data", action="store_true"
    )
    actions.add_argument("--finalize", help="Write final RF model", action="store_true")

    annotate_params = parser.add_argument_group("Annotate features parameters")
    annotate_params.add_argument(
        "--impute_features_no_variant_type",
        help="If set, feature imputation is NOT split by variant type.",
        action="store_true",
    )
    annotate_params.add_argument(
        "--n_partitions",
        help="Desired number of partitions for annotated RF Table",
        type=int,
        default=5000,
    )

    rf_params = parser.add_argument_group("Random Forest parameters")
    rf_params.add_argument(
        "--fp_to_tp",
        help="Ratio of FPs to TPs for training the RF model. If 0, all training examples are used. (default=1.0)",
        default=1.0,
        type=float,
    )
    rf_params.add_argument(
        "--test_intervals",
        help='The specified interval(s) will be held out for testing and evaluation only. (default to "chr20")',
        nargs="+",
        type=str,
        default="chr20",
    )
    rf_params.add_argument(
        "--num_trees",
        help="Number of trees in the RF model. (default=500)",
        default=500,
        type=int,
    )
    rf_params.add_argument(
        "--max_depth",
        help="Maxmimum tree depth in the RF model. (default=5)",
        default=5,
        type=int,
    )

    training_params = parser.add_argument_group("Training data parameters")
    training_params.add_argument(
        "--adj", help="Use adj genotypes.", action="store_true"
    )
    training_params.add_argument(
        "--vqsr_training", help="Use VQSR training examples", action="store_true"
    )
    training_params.add_argument(
        "--vqsr_type",
        help="If a string is provided the VQSR training annotations will be used for training.",
        default="alleleSpecificTrans",
        choices=["classic", "alleleSpecific", "alleleSpecificTrans"],
        type=str,
    )
    training_params.add_argument(
        "--no_transmitted_singletons",
        help="Do not use transmitted singletons for training.",
        action="store_true",
    )
    training_params.add_argument(
        "--no_inbreeding_coeff",
        help="Train RF without inbreeding coefficient as a feature.",
        action="store_true",
    )
    training_params.add_argument(
        "--filter_centromere_telomere",
        help="Train RF without centromeres and telomeres.",
        action="store_true",
    )

    args = parser.parse_args()

    if not args.run_hash and not args.train_rf and args.apply_rf:
        sys.exit(
            "Error: --run_hash is required when running --apply_rf without running --train_rf too."
        )

    if args.run_hash and args.train_rf:
        sys.exit(
            "Error: --run_hash and --train_rf are mutually exclusive. --train_rf will generate a run hash."
        )

    if args.slack_channel:
        with slack_notifications(slack_token, args.slack_channel):
            main(args)
    else:
        main(args)