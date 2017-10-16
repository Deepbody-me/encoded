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
from elasticsearch.helpers import scan
from snovault import DBSESSION, COLLECTIONS
#from snovault.storage import (
#    TransactionRecord,
#)
from snovault.elasticsearch.indexer import (
    SEARCH_MAX,
    IndexerState,
    Indexer,
    all_uuids,
)

from snovault.elasticsearch.interfaces import (
    ELASTIC_SEARCH,
    SNP_SEARCH_ES,
    INDEXER,
)

log = logging.getLogger(__name__)


# Region indexer 2.0
# Desired:  Cycle every 1 hour
# Plan:
# 1) get list of uuids of primary indexer of ALLOWED_FILE_FORMATS
# 2) walk through uuid list querying encoded for each doc[embedded]
#    3) Walk through embedded files
#       4) If file passes required tests (e.g. bed, released, DNase narrowpeak) AND not in regions_es, put in regions_es
#       5) If file does not pass tests                                          AND     IN regions_es, remove from regions_es
# Needed:
# regulomeDb versions of ENCODED_ALLOWED_FILE_FORMATS, ENCODED_ALLOWED_STATUSES, add_encoded_to_regions_es()
#
# TODO: Change the name to regions_indexer.py and spread through the ini's

# Species and references being indexed
SUPPORTED_ASSEMBLIES = ['hg19', 'mm10', 'mm9', 'GRCh38']

ENCODED_ALLOWED_FILE_FORMATS = [ 'bed']
ENCODED_ALLOWED_STATUSES = [ 'released' ]
RESIDENT_DATASETS_KEY = 'resident_datasets'  # in regions_es, keep track of what datsets are resident in one place

# assay_term_name: file_property: allowed list
# datasets: 12283 datasets: ChIP-seq, DNase, eCLIP  Release only 8775  &files.file_type=bed+narrowPeak 5969
# files: released: 'optimal idr thresholded peaks' 'GRCh38', 'hg19', 'mm10' 2852
#        released: 'bed narrowPeak', lab.name=gene-yeo                       666
#        released: 'bed narrowPeaks', analysis_step_version.name=dnase-call-hotspots-pe-step-v-2-0 240, se: 1157;
# expect to index: 2852+666+240+1157=4915 so approx: 4900.
# curl http://region-search-test-v5.instance.encodedcc.org:9200/resident_datasets/_count/?pretty 4964
ENCODED_REGION_REQUIREMENTS = {
    'ChIP-seq': {
        'output_type': ['optimal idr thresholded peaks'],
        'file_format': ['bed']
    },
    'DNase-seq': {
        'file_type': ['bed narrowPeak'],
        'file_format': ['bed']
    },
    'eCLIP': {
        'file_type': ['bed narrowPeak'],
        'file_format': ['bed']
    }
}


def includeme(config):
    config.add_route('index_region', '/index_region')
    config.scan(__name__)
    config.add_route('_regionindexer_state', '/_regionindexer_state')
    registry = config.registry
    registry['region'+INDEXER] = RegionIndexer(registry)

def tsvreader(file):
    reader = csv.reader(file, delimiter='\t')
    for row in reader:
        yield row

# Mapping should be generated dynamically for each assembly type


def get_mapping(assembly_name='hg19'):
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


def index_settings():
    return {
        'index': {
            'number_of_shards': 1
        }
    }


def all_regionable_dataset_uuids(registry):
    return list(all_uuids(registry, types=["experiment"]))


def gather_uuids(hits):
    """
    Since es returns a genorator from scan(), use this to boil it down to uuids
    """
    for hit in hits:
        yield hit['_id']


#def encoded_regionable_datasets(request, restrict_to_assays=[]):
#    # TODO: reduce by not released?
#   query = "select distinct(resources.rid) from resources, propsheets where resources.rid = propsheets.rid and resources.item_type='experiment'"  # ?? 'dataset' ??
#   if len(restrict_to_assays) == 1:
#       query += " and propsheets.properties->>'assay_term_name' = '%s'" % restrict_to_assays[0]
#   elif len(restrict_to_assays) > 1:
#       assays = "('%s'" % (restrict_to_assays[0])
#       for assay in restrict_to_assays[1:]:
#           assays += ", '%s'" % assay
#       assays += ")"
#       query += " and propsheets.properties->>'assay_term_name' IN %s" % assays
#   stmt = text(query + ";")
#   connection = request.registry[DBSESSION].connection()
#   uuids = connection.execute(stmt)
#   return [str(item[0]) for item in uuids]

def encoded_regionable_datasets(request, restrict_to_assays=[]):
    '''return list of all dataset uuids eligible for regions'''
    # reverse engineered search.py to get this boied down version.
    #{
    #"filter":
    #    {
    #    "and":
    #        {
    #        "filters":
    #            [
    #            {"terms": {"principals_allowed.view": ["system.Everyone"]}},
    #            {"terms": {"embedded.@type.raw": ["Experiment"]}},
    #            {"terms": {"embedded.assay_title.raw": ["ChIP-seq", "DNase-seq","eCLIP"]}}
    #            ]
    #        }
    #    },
    #"query": {"match_all": {}},
    #"_source": ["uuid"]
    #}
    #curl -XGET http://localhost:9200/snovault/_search?size=1000 -H 'Content-Type: application/json' -d'{"filter": {"and": {"filters": [ {"terms": {"principals_allowed.view": ["system.Everyone"]}}, {"terms": {"embedded.@type.raw": ["Experiment"]}}, {"terms": {"embedded.assay_title.raw": ["ChIP-seq", "total RNA-seq"]}}]}},"query": {"match_all": {}},"_source": ["uuid"]}'

    encoded_es = request.registry[ELASTIC_SEARCH]
    encoded_INDEX = request.registry.settings['snovault.elasticsearch.index']

    # basics... only want uuids
    query = {'filter': {'and': {'filters': [ {'terms': {'principals_allowed.view': ['system.Everyone']}}]}},'query': {'match_all': {}},'_source': ['uuid']}
    # Only experiments
    term = {'terms': {'embedded.@type.raw': ['Experiment']}}
    query['filter']['and']['filters'].append(term)
    # Only released experiments
    term = {'terms':{'embedded.status.raw': ['released']}}
    query['filter']['and']['filters'].append(term)
    # restrict to certain assays
    if len(restrict_to_assays) > 0:
        term = {'terms':{'embedded.assay_title.raw': restrict_to_assays}}
        query['filter']['and']['filters'].append(term)
    #log.war(query)  # make sure your query is what you think it is
    #es_results = encoded_es.search(body=query, index=encoded_INDEX, search_type='count')
    #if es_results['hits']['total'] <= 0:
    #    log.warn('encoded_regionable_datasets returned 0 uuids from encoded_es')
    #    return []
    hits = scan(encoded_es, query=query, index=encoded_INDEX, preserve_order=False)
    uuids = gather_uuids(hits)
    return list(uuids)  # Ensures all get returned from generator


class RegionIndexerState(IndexerState):
    # Accepts handoff of uuids from primary indexer. Keeps track of uuids and secondary_indexer state by cycle.
    def __init__(self, es, key):
        super(RegionIndexerState, self).__init__(es,key, title='regions')
        self.files_added_set    = self.title + '_files_added'
        self.files_dropped_set  = self.title + '_files_dropped'
        self.success_set        = self.files_added_set
        self.cleanup_last_cycle.extend([self.files_added_set,self.files_dropped_set])  # Clean up at beginning of next cycle
        # DO NOT INHERIT! These keys are for passing on to other indexers
        self.followup_prep_list = None                        # No followup to a following indexer
        self.staged_cycles_list = None                        # Will take all of primary self.staged_for_regions_list
        self.force_uuids        = self.title + "_force_dataset_uuids" # uuids to force a reindex on.

    def file_added(self, uuid):
        self.list_extend(self.files_added_set, [uuid])

    def file_dropped(self, uuid):
        self.list_extend(self.files_added_set, [uuid])

    def finish_cycle(self, state, errors=None):
        '''Every indexing cycle must be properly closed.'''

        if errors:  # By handling here, we avoid overhead and concurrency issues of uuid-level accounting
            self.add_errors(errors)

        # cycle-level accounting so todo => done => last in this function
        #self.rename_objs(self.todo_set, self.done_set)
        #done_count = self.get_count(self.todo_set)
        cycle_count = state.pop('cycle_count', None)
        self.rename_objs(self.todo_set, self.last_set)

        added = self.get_count(self.files_added_set)
        dropped = self.get_count(self.files_dropped_set)
        state['indexed'] = added + dropped

        #self.rename_objs(self.done_set, self.last_set)   # cycle-level accounting so todo => done => last in this function
        self.delete_objs(self.cleanup_this_cycle)
        state['status'] = 'done'
        state['cycles'] = state.get('cycles', 0) + 1
        state['cycle_took'] = self.elapsed('cycle')

        self.put(state)

        return state

    def priority_cycle(self, request):
        '''Initial startup, override, or interupted prior cycle can all lead to a priority cycle.
           returns (priority_type, uuids).'''
        # Not yet started?
        initialized = self.get_obj("indexing")  # http://localhost:9200/snovault/meta/indexing
        if not initialized:
            self.delete_objs([self.override, self.staged_for_regions_list])
            state = self.get()
            state['status'] = 'uninitialized'
            self.put(state)
            return ("uninitialized", [])  # primary indexer will know what to do and secondary indexer should do nothing yet

        # Is a full indexing underway
        primary_state = self.get_obj("primary_indexer")
        if primary_state.get('cycle_count',0) > SEARCH_MAX:
            return ("uninitialized", [])

        # Rare call for indexing all...
        override = self.get_obj(self.override)
        if override:
            self.delete_objs([self.override,self.force_uuids])
            # So we want all the appropriate dataset uuids, but from es, not postgres
            # TODO: make this work
            assays = list(ENCODED_REGION_REQUIREMENTS.keys())
            uuids = []
            try:
                uuids = encoded_regionable_datasets(request, assays)
            except:
                # TODO: mention error?
                uuids = list(all_regionable_dataset_uuids(request.registry))  #### query all uuids
            log.warn('%s override doing all %d with force' % (self.state_id, len(uuids)))
            return ("reindex", uuids)

        # Rarer call to force reindexing specific set
        uuids = self.get_list(self.force_uuids)
        if len(uuids) > 0:
            self.delete_objs([self.force_uuids])
            log.warn('%s override doing selected %d with force' % (self.state_id, len(uuids)))
            return ("reindex", uuids)

        if self.get().get('status', '') == 'indexing':
            uuids = self.get_list(self.todo_set)
            log.warn('%s restarting on %d datasets' % (self.state_id, len(uuids)))
            return ("restart", uuids)

        return ("normal", [])

    def get_one_cycle(self, request):
        '''Reutrns set of uuids to do this cycle and whether they should be forced.'''

        # never indexed, request for full reindex?
        (status, uuids) = self.priority_cycle(request)
        if status == 'uninitialized':
            return ([], False)            # Until primary_indexer has finished, do nothing!

        if len(uuids) > 0:
            if status == "reindex":
                return (uuids, True)
            if status == "restart":  # Restart is fine... just do the uuids over again
                #return (uuids, False)
                log.warn('%s skipping this restart' % (self.state_id))
                return ([], False)
        assert(uuids == [])

        # Normal case, look for uuids staged by primary indexer
        staged_list = self.get_list(self.staged_for_regions_list)
        if not staged_list or staged_list == []:
            return ([], False)            # Nothing to do!
        self.delete_objs([self.staged_for_regions_list])  # TODO: tighten this by adding a locking semaphore

        # we don't need no stinking xmins... just take the whole set of uuids
        uuids = []
        for val in staged_list:
            if val.startswith("xmin:"):
                continue
            else:
                uuids.append(val)

        if len(uuids) > 0: #500:  # some arbitrary cutoff.
            # There is an efficiency trade off examining many non-dataset uuids
            # # vs. the cost of eliminating those uuids from the list ahead of time.
            assays = list(ENCODED_REGION_REQUIREMENTS.keys())
            uuids = list(set(encoded_regionable_datasets(request, assays)).intersection(uuids))
            uuid_count = len(uuids)

        return (list(set(uuids)),False)  # Only unique uuids

    def display(self):
        display = super(RegionIndexerState, self).display()
        display['staged to process'] = self.get_count(self.staged_cycles_list)
        display['files added'] = self.get_count(self.files_added_set)
        display['files dropped'] = self.get_count(self.files_dropped_set)
        # very rare
        count = self.get_count(self.force_uuids)
        if count > 0:
            display['reqested datasets to force'] = count
        return display


@view_config(route_name='_regionindexer_state', request_method='GET', permission="index")
def regions_indexer_show_state(request):
    encoded_es = request.registry[ELASTIC_SEARCH]
    encoded_INDEX = request.registry.settings['snovault.elasticsearch.index']
    regions_es    = request.registry[SNP_SEARCH_ES]
    state = RegionIndexerState(encoded_es,encoded_INDEX)  # Consider putting this in regions es instead of encoded es

    if request.params.get("reindex","false") == 'all':
        state.request_reindex()
        request.query_string = ''

    display = state.display()

    try:
        count = regions_es.count(index=RESIDENT_DATASETS_KEY, doc_type='default').get('count',0)
        if count:
            display['files in index'] = count
    except:
        display['files in index'] = 'Not Found'
        pass

    try:
        import requests
        r = requests.get(request.host_url + '/_fileindexer')
        #subreq = Request.blank('/_fileindexer')
        #result = request.invoke_subrequest(subreq)
        result = json.loads(r.text)
        #result = request.embed('_fileindexer')
        result['current'] = display
    except:
        result = display

    # always return raw json
    if len(request.query_string) > 0:
        request.query_string = "&format=json"
    else:
        request.query_string = "format=json"
    return display


@view_config(route_name='index_region', request_method='POST', permission="index")
def index_regions(request):
    encoded_es = request.registry[ELASTIC_SEARCH]
    encoded_INDEX = request.registry.settings['snovault.elasticsearch.index']
    request.datastore = 'elasticsearch'  # Let's be explicit
    dry_run = request.json.get('dry_run', False)
    indexer = request.registry['region'+INDEXER]
    uuids = []


    # keeping track of state
    state = RegionIndexerState(encoded_es,encoded_INDEX)
    result = state.get_initial_state()

    (uuids, force) = state.get_one_cycle(request)

    uuid_count = len(uuids)
    if uuid_count > 0 and not dry_run:
        log.warn("Region indexer started on %d datasets(s)" % uuid_count) # DEBUG set back to info when done

        result = state.start_cycle(uuids, result)
        errors = indexer.update_objects(request, uuids, force)
        result = state.finish_cycle(result, errors)
        log.info("Region indexer added %d file(s)" % result['indexed']) # TODO: change to info
        # cycle_took: "2:31:55.543311" reindex all with force

    return result


class RegionIndexer(Indexer):
    def __init__(self, registry):
        super(RegionIndexer, self).__init__(registry)
        self.encoded_es    = registry[ELASTIC_SEARCH]    # yes this is self.es but we want clarity
        self.encoded_INDEX = registry.settings['snovault.elasticsearch.index']  # yes this is self.index, but clarity
        self.regions_es    = registry[SNP_SEARCH_ES]
        self.residents_index = RESIDENT_DATASETS_KEY
        self.state = RegionIndexerState(self.encoded_es,self.encoded_INDEX)  # WARNING, race condition is avoided because there is only one worker

    def get_from_es(request, comp_id):
        '''Returns composite json blob from elastic-search, or None if not found.'''
        return None

    def update_object(self, request, dataset_uuid, force):
        request.datastore = 'elasticsearch'  # Let's be explicit

        # TODO: if force then drop current index contents?

        try:
            dataset = self.encoded_es.get(index=self.encoded_INDEX, id=str(dataset_uuid)).get('_source',{}).get('embedded')
        except:
            log.warn("dataset is not found for uuid: %s",dataset_uuid)
            # Not an error if it wasn't found.
            return

        # TODO: add case where files are never dropped (when demos share test server this might be necessary)
        if not self.encoded_candidate_dataset(dataset):
            #log.debug("dataset is not candidate: %s",dataset_uuid)
            return  # Note that if a dataset is no longer a candidate but it had files in regions es, they won't get removed.
        log.debug("dataset is a candidate: %s", dataset['accession'])

        assay_term_name = dataset.get('assay_term_name')
        if assay_term_name is None:
            return

        files = dataset.get('files',[])
        for afile in files:
            if afile.get('file_format') not in ENCODED_ALLOWED_FILE_FORMATS:
                continue  # Note: if file_format changed to not allowed but file already in regions es, it doesn't get removed.

            file_uuid = afile['uuid']

            if self.encoded_candidate_file(afile, assay_term_name):

                using = ""
                if force:
                    using = "with FORCE"
                    #log.debug("file is a candidate: %s %s", afile['accession'], using)
                    self.remove_from_regions_es(file_uuid)  # remove all regions first
                else:
                    #log.debug("file is a candidate: %s", afile['accession'])
                    if self.in_regions_es(file_uuid):
                        continue

                if self.add_encoded_file_to_regions_es(request, afile):
                    log.info("added file: %s %s %s", dataset['accession'], afile['href'], using)
                    self.state.file_added(file_uuid)

            else:
                if self.remove_from_regions_es(file_uuid):
                    log.info("dropped file: %s %s %s", dataset['accession'], afile['@id'], using)
                    self.state.file_dropped(file_uuid)

        # TODO: gather and return errors

    def encoded_candidate_file(self, afile, assay_term_name):
        '''returns True if an encoded file should be in regions es'''
        if afile.get('status', 'imagined') not in ENCODED_ALLOWED_STATUSES:
            return False
        if afile.get('href') is None:
            return False

        assembly = afile.get('assembly','unknown')
        if assembly == 'mm10-minimal':        # Treat mm10-minimal as mm10
            assembly = 'mm10'
        if assembly not in SUPPORTED_ASSEMBLIES:
            return False

        required = ENCODED_REGION_REQUIREMENTS.get(assay_term_name,{})
        if not required:
            return False

        for prop in list(required.keys()):
            val = afile.get(prop)
            if val is None:
                return False
            if val not in required[prop]:
                return False

        return True

    def encoded_candidate_dataset(self, dataset):
        '''returns True if an encoded dataset may have files that should be in regions es'''
        if 'Experiment' not in dataset['@type']:  # Only experiments?
            return False

        if dataset.get('assay_term_name','unknown') not in list(ENCODED_REGION_REQUIREMENTS.keys()):
            return False

        if len(dataset.get('files',[])) == 0:
            return False
        return True

    def in_regions_es(self, id):
        '''returns True if an id is in regions es'''
        #return False # DEBUG
        try:
            doc = self.regions_es.get(index=self.residents_index, doc_type='default', id=str(id)).get('_source',{})
            if doc:
                return True
        except NotFoundError:
            return False
        except:
            #raise
            pass

        return False


    def remove_from_regions_es(self, id):
        '''Removes all traces of an id (usually uuid) from region search elasticsearch index.'''
        #return True # DEBUG
        try:
            doc = self.regions_es.get(index=self.residents_index, doc_type='default', id=str(id)).get('_source',{})
            if not doc:
                return False
        except:
            # TODO: add a warning?
            return False  # Will try next cycle

        for chrom in doc['chroms']:
            try:
                self.regions_es.delete(index=chrom, doc_type=doc['assembly'], id=str(uuid))
            except:
                # TODO: add a warning.
                return False # Will try next cycle

        try:
            self.regions_es.delete(index=self.residents_index, doc_type='default', id=str(uuid))
        except:
            # TODO: add a warning.
            return False # Will try next cycle

        return True


    def add_to_regions_es(self, id, assembly, regions):
        '''Given regions from some source (most likely encoded file) loads the data into region search es'''
        #return True # DEBUG
        for key in regions:
            doc = {
                'uuid': str(id),
                'positions': regions[key]
            }
            # Could be a chrom never seen before!
            if not self.regions_es.indices.exists(key):
                self.regions_es.indices.create(index=key, body=index_settings())

            if not self.regions_es.indices.exists_type(index=key, doc_type=assembly):
                self.regions_es.indices.put_mapping(index=key, doc_type=assembly, body=get_mapping(assembly))

            self.regions_es.index(index=key, doc_type=assembly, body=doc, id=str(id))

        # Now add dataset to residency list
        doc = {
            'uuid': str(id),
            'assembly': assembly,
            'chroms': list(regions.keys())
        }
        # Make sure there is an index set up to handle whether uuids are resident
        if not self.regions_es.indices.exists(self.residents_index):
            self.regions_es.indices.create(index=self.residents_index, body=index_settings())

        if not self.regions_es.indices.exists_type(index=self.residents_index, doc_type='default'):
            mapping = {'default': {"_all":    {"enabled": False},"_source": {"enabled": True},}}
            self.regions_es.indices.put_mapping(index=self.residents_index, doc_type='default', body=mapping)

        self.regions_es.index(index=self.residents_index, doc_type='default', body=doc, id=str(id))
        return True

    def add_encoded_file_to_regions_es(self, request, afile):
        '''Given an encoded file object, reads the file to create regions data then loads that into region search es.'''
        #return True # DEBUG

        assembly = afile.get('assembly','unknown')
        if assembly == 'mm10-minimal':        # Treat mm10-minimal as mm10
            assembly = 'mm10'
        if assembly not in SUPPORTED_ASSEMBLIES:
            return False

        urllib3.disable_warnings()
        http = urllib3.PoolManager()
        r = http.request('GET', request.host_url + afile['href'])
        if r.status != 200:
            return False
        file_in_mem = io.BytesIO()
        file_in_mem.write(r.data)
        file_in_mem.seek(0)
        r.release_conn()

        file_data = {}
        if afile['file_format'] == 'bed':
            with gzip.open(file_in_mem, mode='rt') as file:
                for row in tsvreader(file):
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
        ### else:  Other file types?

        if file_data:
            return self.add_to_regions_es(afile['uuid'], assembly, file_data)

        return False
