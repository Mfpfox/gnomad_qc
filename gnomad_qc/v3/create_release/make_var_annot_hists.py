import argparse
import json
import logging

import hail as hl
from gnomad.resources.resource_utils import DataException
from gnomad.utils.annotations import (create_frequency_bins_expr,
                                      get_annotations_hists)
from gnomad.utils.file_utils import file_exists
from gnomad.utils.slack import slack_notifications
from gnomad_qc.slack_creds import slack_token
from gnomad_qc.v3.resources.constants import CURRENT_RELEASE
from gnomad_qc.v3.resources.release import (annotation_hists_path,
                                            qual_hists_json_path,
                                            release_ht_path)


logging.basicConfig(
    format="%(asctime)s (%(name)s %(lineno)s): %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


LOG10_ANNOTATIONS = ["AS_VarDP", "QUALapprox", "AS_QUALapprox"]
"""
List of annotations to log scale when creating histograms. 
"""


def create_frequency_bins_expr_inbreeding(
        AF: hl.expr.NumericExpression
) -> hl.expr.StringExpression:
    """
    Creates bins for frequencies in preparation for aggregating QUAL by frequency bin.

    Uses bins of < 0.0005 and >= 0.0005

    NOTE: Frequencies should be frequencies from raw data.
    Used when creating site quality distribution json files.

    :param AC: Field in input that contains the allele count information
    :param AF: Field in input that contains the allele frequency information
    :return: Expression containing bin name
    :rtype: hl.expr.StringExpression
    """
    bin_expr = (
        hl.case()
            .when(AF < 0.0005, "under_0.0005")
            .when((AF >= 0.0005) & (AF <= 1), "over_0.0005")
            .default(hl.null(hl.tstr))
    )
    return bin_expr


def main(args):
    hl.init(default_reference='GRCh38', log='/variant_histograms.log')

    ht = hl.read_table(release_ht_path())
    # NOTE: histogram aggregations are done on the entire callset (not just PASS variants), on raw data

    hist_dict = ANNOTATIONS_HISTS
    hist_dict['MQ'] = (20, 60, 40) # Boundaries changed for v3, but could be a good idea to settle on a standard
    hist_ranges_expr = get_annotations_hists(
        ht,
        ANNOTATIONS_HISTS
    )

    # NOTE: run the following code in a first pass to determine bounds for metrics
    # Evaluate minimum and maximum values for each metric of interest
    # This doesn't need to be run unless the defaults do not result in nice-looking histograms.
    if args.first_pass:
        minmax_dict = {}
        for metric in hist_ranges_expr.keys():
            minmax_dict[metric] = hl.struct(min=hl.agg.min(ht[metric]), max=hl.if_else(hl.agg.max(ht[metric])<1e10, hl.agg.max(ht[metric]), 1e10))
        minmax = ht.aggregate(hl.struct(**minmax_dict))
        print(minmax)
    else:
        # Aggregate hists over hand-tooled ranges
        hists = ht.aggregate(
            hl.array(
                [hist_expr.annotate(metric=hist_metric) for hist_metric, hist_expr in hist_ranges_expr.items()]
            ).extend(
                hl.array(
                    hl.agg.group_by(
                        create_frequency_bins_expr(
                            AC=ht.freq[1].AC,
                            AF=ht.freq[1].AF
                        ),
                        hl.agg.hist(hl.log10(ht.info.QUALapprox), 1, 10, 36)
                    )
                ).map(
                    lambda x: x[1].annotate(metric=x[0])
                )
            ),
            _localize=False
        )

        with hl.hadoop_open(qual_hists_json_path(CURRENT_RELEASE), 'w') as f:
            f.write(hl.eval(hl.json(hists)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--first_pass', help='Determine min/max values for each variant metric and prints them to stdout (to be used in hand-tooled histogram ranges). Note that this should only be run if the defaults do not result in well-behaved histograms.', action='store_true')
    parser.add_argument('--slack_channel', help='Slack channel to post results and notifications to.')
    parser.add_argument('--overwrite', help='Overwrite data', action='store_true')
    args = parser.parse_args()

    if args.slack_channel:
        try_slack(args.slack_channel, main, args)
    else:
        main(args)
