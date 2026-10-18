"""
Utils for dealing with arxiv API and related processing
"""

import time
import logging
import threading
import urllib.request
import feedparser
from collections import OrderedDict

logger = logging.getLogger(__name__)

# the arXiv API terms ask for at most one request every 3 seconds
MIN_REQUEST_INTERVAL = 3.0
_last_request = 0.0
_request_lock = threading.Lock()

def get_response(search_query, start_index=0, max_results=100, sort_by='lastUpdatedDate', timeout=60):
    """ pings arxiv.org API to fetch a batch of (by default 100) papers """
    global _last_request
    # fetch raw response
    base_url = 'https://export.arxiv.org/api/query?'
    add_url = 'search_query=%s&sortBy=%s&start=%d&max_results=%d' % (search_query, sort_by, start_index, max_results)
    search_query = base_url + add_url
    logger.debug(f"Searching arxiv for {search_query}")
    with _request_lock:
        wait = MIN_REQUEST_INTERVAL - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(search_query, timeout=timeout) as url:
                response = url.read()
        finally:
            _last_request = time.time()

    if url.status != 200:
        logger.error(f"arxiv did not return status 200 response")

    return response

def search(search_query, max_results=25):
    """ live search of all of arXiv, search_query as from aslite.search.to_arxiv_query; papers in relevance order """
    return parse_response(get_response(search_query, max_results=max_results, sort_by='relevance', timeout=20))

def encode_feedparser_dict(d):
    """ helper function to strip feedparser objects using a deep copy """
    if isinstance(d, feedparser.FeedParserDict) or isinstance(d, dict):
        return {k: encode_feedparser_dict(d[k]) for k in d.keys()}
    elif isinstance(d, list):
        return [encode_feedparser_dict(k) for k in d]
    else:
        return d

def parse_arxiv_url(url):
    """
    examples is http://arxiv.org/abs/1512.08756v2
    we want to extract the raw id (1512.08756) and the version (2)
    """
    ix = url.rfind('/')
    assert ix >= 0, 'bad url: ' + url
    idv = url[ix+1:] # extract just the id (and the version)
    parts = idv.split('v')
    assert len(parts) == 2, 'error splitting id and version in idv string: ' + idv
    return idv, parts[0], int(parts[1])

def parse_response(response):

    out = []
    parse = feedparser.parse(response)
    for e in parse.entries:
        j = encode_feedparser_dict(e)
        # extract / parse id information
        idv, rawid, version = parse_arxiv_url(j['id'])
        j['_idv']= idv
        j['_id'] = rawid
        j['_version'] = version
        j['_time'] = time.mktime(j['updated_parsed'])
        j['_time_str'] = time.strftime('%b %d %Y', j['updated_parsed'])
        # delete apparently spurious and redundant information
        del j['summary_detail']
        del j['title_detail']
        out.append(j)

    return out

def filter_latest_version(idvs):
    """
    for each idv filter the list down to only the most recent version
    """

    pid_to_v = OrderedDict()
    for idv in idvs:
        pid, v = idv.split('v')
        pid_to_v[pid] = max(int(v), pid_to_v.get(pid, 0))

    filt = [f"{pid}v{v}" for pid, v in pid_to_v.items()]
    return filt