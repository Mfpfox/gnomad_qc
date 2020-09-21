from gnomad.resources.resource_utils import TableResource

ANNOTATIONS_ROOT = "gs://gnomad/annotations/hail-0.2/ht/genomes_v3"


def get_info(split: bool = True) -> TableResource:
    """
    Gets the gnomAD v3 info TableResource

    :param split: Whether to return the split or multi-allelic version of the resource
    :return: gnomAD v3 info TableResource
    """
    path = '{}/gnomad_genomes_v3_info{}.ht'.format(
        ANNOTATIONS_ROOT,
        '.split' if split else ''
    )
    return TableResource(path)


last_END_position = TableResource(f'{ANNOTATIONS_ROOT}/gnomad_genomes_v3_last_END_positions.ht')
freq = TableResource(f'{ANNOTATIONS_ROOT}/gnomad_genomes_v3.frequencies.ht')
qual_hist = TableResource(f'{ANNOTATIONS_ROOT}/gnomad_genomes_v3.qual_hists.ht')
vep = TableResource(f'{ANNOTATIONS_ROOT}/gnomad_genomes_v3_vep.ht')
info_vcf_path = f'{ANNOTATIONS_ROOT}/gnomad_genomes_v3_info.vcf.bgz'
qc_ac = TableResource(f'{ANNOTATIONS_ROOT}/gnomad_genomes_qc_ac.ht')
fam_stats = TableResource(f'{ANNOTATIONS_ROOT}/gnomad_genomes_qc_fam_stats.ht')
