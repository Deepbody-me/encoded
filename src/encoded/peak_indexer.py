import urllib3
import io
import gzip
import csv
import logging
import collections
from pyramid.view import view_config
from sqlalchemy.sql import text
from elasticsearch.exceptions import (
    NotFoundError
)
from snovault import DBSESSION, COLLECTIONS
from snovault.storage import (
    TransactionRecord,
)
from snovault.elasticsearch.indexer import all_uuids
from snovault.elasticsearch.interfaces import (
    ELASTIC_SEARCH,
    SNP_SEARCH_ES,
)

import requests


SEARCH_MAX = 99999  # OutOfMemoryError if too high
log = logging.getLogger(__name__)


# hashmap of assays and corresponding file types that are being indexed
_INDEXED_DATA = {
    'ChIP-seq': {
        'output_type': ['optimal idr thresholded peaks'],
    },
    'DNase-seq': {
        'file_type': ['bed narrowPeak']
    },
    'eCLIP': {
        'file_type': ['bed narrowPeak']
    },
    'RNA-seq': {
        'output_type': ['gene quantifications']
    }
}

# Species and references being indexed
_ASSEMBLIES = ['hg19', 'mm10', 'mm9', 'GRCh38']


def includeme(config):
    config.add_route('index_file', '/index_file')
    config.scan(__name__)


def tsvreader(file):
    reader = csv.reader(file, delimiter='\t')
    for row in reader:
        yield row

# Mapping should be generated dynamically for each assembly type


def get_peak_mapping(assembly_name='hg19'):
    ''' mapping for bed/peak interval '''
    return {
        assembly_name: {
            '_all': {
                'enabled': False
            },
            '_source': {
                'enabled': True
            },
            'properties': {
                'uuid': {
                    'type': 'string',
                    'index': 'not_analyzed'
                },
                'positions': {
                    'type': 'nested',
                    'properties': {
                        'start': {
                            'type': 'long'
                        },
                        'end': {
                            'type': 'long'
                        }
                    }
                }
            }
        }
    }


def get_tnx_mapping(assembly_name='hg19'):
    ''' Mapping for transcription level by gene '''
    return {
        assembly_name: {
            '_all': {
                'enabled': False
            },
            '_source': {
                'enabled': True
            },
            'properties': {
                'uuid': {
                    'type': 'string',
                    'index': 'not_analyzed'
                },
                'expression': {
                    'type': 'nested',
                    'properties': {
                        'transcript_id': {
                            'type': 'string'
                        },
                        'gene_id': {
                            'type': 'string'
                        },
                        'tpm': {
                            'type': 'long'
                        },
                        'fpkm': {
                            'type': 'long'
                        }
                    }
                }
            }
        }
    }


def index_settings():
    return {
        'index': {
            'number_of_shards': 1
        }
    }


def get_assay_name(accession, request):
    '''
    Input file accession and returns assay_term_name and assay_title of the experiment the file
    belongs to
    '''
    context = request.embed(accession)
    return (context.get('assay_term_name', None), context.get('assay_title', None))


def all_file_uuids_by_type(request, type='bed'):
    stmt = text("select distinct(resources.rid) from resources, propsheets where resources.rid = propsheets.rid and resources.item_type='file' and propsheets.properties->>'file_format' = '%s' and properties->>'status' = 'released';" % type)
    connection = request.registry[DBSESSION].connection()
    uuids = connection.execute(stmt)
    return [str(item[0]) for item in uuids]


def all_dataset_uuids(request):
    datasets = request.registry[COLLECTIONS]['datasets']
    return [uuid for uuid in datasets]


def all_experiment_uuids(request):
    return list(all_uuids(request.registry, types='experiment'))


def index_peaks(uuid, request, ftype='bed'):
    """
    Indexes bed or tsv files in elasticsearch index
    """
    context = request.embed('/', str(uuid), '@@object')

    if 'assembly' not in context:
        return

    assembly = context['assembly']

    # Treat mm10-minimal as mm1
    if assembly == 'mm10-minimal':
        assembly = 'mm10'

    if 'File' not in context['@type'] or 'dataset' not in context:
        return

    if 'status' not in context or context['status'] != 'released':
        return

    # Index human data for now
    if assembly not in _ASSEMBLIES:
        return

    (assay_term_name, assay_title) = get_assay_name(context['dataset'], request)
    if assay_term_name is None or isinstance(assay_term_name, collections.Hashable) is False:
        return

    flag = False

    for k, v in _INDEXED_DATA.get(assay_term_name, {}).items():
        if k in context and context[k] in v:
            if 'file_format' in context and context['file_format'] == ftype:
                flag = True
                break
    if not flag:
        return

    if request.host_url == 'http://localhost':
        host_url = request.host_url + ':8000'
        test_files = ['/static/test/peak_indexer/ENCFF002COS.bed.gz',
                      '/static/test/peak_indexer/ENCFF296FFD.tsv',
                      '/static/test/peak_indexer/ENCFF000PAR.bed.gz']
        if context['submitted_file_name'] not in test_files:
            return
        # assume we are running in dev-servers
    else:
        host_url = request.host_url

    href = host_url + context['href']
    if ftype == 'bed':
        index_bed(href, request, context, assembly)
    elif ftype == 'tsv':
        index_tsv(href, request, context, assembly)


def index_tsv(href, request, context, assembly):

    file_data = dict()
    es = request.registry.get(SNP_SEARCH_ES, None)
    annotation = context['genome_annotation']

    dlreq = requests.get(href)

    comp = io.StringIO()
    comp.write(dlreq.text)
    comp.seek(0)


    for row in tsvreader(comp):
        transcript_id, gene_id, tpm, fpkm = row[0], row[1], float(row[5]), float(row([6]))
        if tpm > 0.0 or fpkm > 0.0:
            payload = {
                'transcript_id': transcript_id,
                'gene_id': gene_id,
                'tpm': tpm,
                'fpkm': fpkm
            }
            if annotation in file_data:
                file_data[annotation].append(payload)
            else:
                file_data[annotation] = [payload]

    for key in file_data:
        doc = {
            'uuid': context['uuid'],
            'expression': file_data[key]
        }
        if not es.indices.exists(key):
            es.indices.create(index=key, body=index_settings())

        if not es.indices.exists_type(index=key, doc_type=assembly):
            es.indices.put_mapping(index=key, doc_type=assembly, body=get_tnx_mapping(assembly))

        es.index(index=key, doc_type=assembly, body=doc, id=context['uuid'])


def index_bed(href, request, context, assembly):

    dlreq = requests.get(href)

    if not dlreq or dlreq.status_code != 200:
        log.warn("File (%s or %s) not found" % (context.get('href',"No href"), context.get('submitted_file_name', 'No submitted file name')))
        return

    comp = io.BytesIO()
    comp.write(dlreq.content)
    comp.seek(0)

    file_data = dict()
    es = request.registry.get(SNP_SEARCH_ES, None)

    import pdb; pdb.set_trace()

    for row in tsvreader(comp):
        chrom, start, end = row[0].lower(), int(row[1]), int(row[2])
        if isinstance(start, int) and isinstance(end, int):
            if chrom in file_data:
                file_data[chrom].append({
                    'start': start + 1,
                    'end': end + 1
                })
            else:
                file_data[chrom] = [{'start': start + 1, 'end': end + 1}]
        else:
            log.warn('positions are not integers, will not index file')

    for key in file_data:
        doc = {
            'uuid': context['uuid'],
            'positions': file_data[key]
        }
        if not es.indices.exists(key):
            es.indices.create(index=key, body=index_settings())

        if not es.indices.exists_type(index=key, doc_type=assembly):
            es.indices.put_mapping(index=key, doc_type=assembly, body=get_peak_mapping(assembly))

        es.index(index=key, doc_type=assembly, body=doc, id=context['uuid'])


@view_config(route_name='index_file', request_method='POST', permission="index")
def index_file(request):
    registry = request.registry
    INDEX = registry.settings['snovault.elasticsearch.index']
    request.datastore = 'database'
    dry_run = request.json.get('dry_run', False)
    recovery = request.json.get('recovery', False)
    record = request.json.get('record', False)
    es = registry[ELASTIC_SEARCH]
    es_peaks = registry[SNP_SEARCH_ES]

    session = registry[DBSESSION]()
    connection = session.connection()
    if recovery:
        query = connection.execute(
            "SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY;"
            "SELECT txid_snapshot_xmin(txid_current_snapshot());"
        )
    else:
        query = connection.execute(
            "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ ONLY, DEFERRABLE;"
            "SELECT txid_snapshot_xmin(txid_current_snapshot());"
        )
    xmin = query.scalar()  # lowest xid that is still in progress

    first_txn = None
    last_xmin = None
    if 'last_xmin' in request.json:
        last_xmin = request.json['last_xmin']
    else:
        try:
            status = es_peaks.get(index='snovault', doc_type='meta', id='peak_indexing')
        except NotFoundError:
            pass
        else:
            last_xmin = status['_source']['xmin']

    result = {
        'xmin': xmin,
        'last_xmin': last_xmin,
    }

    if last_xmin is None:
        result['types'] = request.json.get('types', None)
        invalidated = list(all_uuids(registry))
    else:
        txns = session.query(TransactionRecord).filter(
            TransactionRecord.xid >= last_xmin,
        )

        invalidated = set()
        updated = set()
        renamed = set()
        max_xid = 0
        txn_count = 0
        for txn in txns.all():
            txn_count += 1
            max_xid = max(max_xid, txn.xid)
            if first_txn is None:
                first_txn = txn.timestamp
            else:
                first_txn = min(first_txn, txn.timestamp)
            renamed.update(txn.data.get('renamed', ()))
            updated.update(txn.data.get('updated', ()))

        result['txn_count'] = txn_count
        if txn_count == 0:
            return result

        es.indices.refresh(index=INDEX)
        res = es.search(index=INDEX, size=SEARCH_MAX, body={
            'filter': {
                'or': [
                    {
                        'terms': {
                            'embedded_uuids': updated,
                            '_cache': False,
                        },
                    },
                    {
                        'terms': {
                            'linked_uuids': renamed,
                            '_cache': False,
                        },
                    },
                ],
            },
            '_source': False,
        })
        if res['hits']['total'] > SEARCH_MAX:
            invalidated = list(all_uuids(registry))
        else:
            referencing = {hit['_id'] for hit in res['hits']['hits']}
            invalidated = referencing | updated
            result.update(
                max_xid=max_xid,
                renamed=renamed,
                updated=updated,
                referencing=len(referencing),
                invalidated=len(invalidated),
                txn_count=txn_count,
                first_txn_timestamp=first_txn.isoformat(),
            )

    if not dry_run:
        error_collection = []
        for ftype in ('bed', 'tsv'):
            invalidated_files = list(set(invalidated).intersection(set(all_file_uuids_by_type(request, ftype))))
            for uuid in invalidated_files:
                uuid_current = uuid
                try:
                    index_peaks(uuid, request, ftype)
                except Exception as e:
                    log.error('Error indexing %s', uuid_current, exc_info=True)
                    error_collection.append(repr(e))
            result['errors'] = error_collection
            result['indexed'] = len(invalidated)
        if record:
            es_peaks.index(index='snovault', doc_type='meta', body=result, id='peak_indexing')
        invalidated_datasets_and_experiments = list(set(invalidated).intersection(set(all_dataset_uuids(request) + all_experiment_uuids(request))))
        registry.notify(AfterIndexedExperimentsAndDatasets(invalidated_datasets_and_experiments, request))
    return result


class AfterIndexedExperimentsAndDatasets(object):
    def __init__(self, object, request):
        self.object = object
        self.request = request
