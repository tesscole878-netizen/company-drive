from fastapi.testclient import TestClient

from test_drive import env, upload
from test_workspace import create_library


def test_file_change_actions_follow_committed_operation(env):
    client = env['clients']['alice']
    lid = create_library(client)
    route = '/api/file-changes'
    cursor = client.get(route).json()['cursor']
    original = upload(client, lid, '计划.txt', b'first')
    ep = f'/api/libraries/{lid}'
    client.put(ep + '/text', json={'path':'计划.txt','etag':original['etag'],'content':'edited'}).raise_for_status()
    stale = client.put(ep + '/text', json={'path':'计划.txt','etag':original['etag'],'content':'lost'})
    assert stale.status_code == 409
    task = client.post('/api/uploads',json={'group_id':lid,'path':'计划.txt','size':3,'fingerprint':'new-version',
                                            'overwrite':True}).json()['id']
    client.patch(f'/api/uploads/{task}',content=b'new',headers={'Upload-Offset':'0'}).raise_for_status()
    client.post(f'/api/uploads/{task}/complete').raise_for_status()
    old = client.get(ep + '/versions', params={'path':'计划.txt'}).json()[-1]['id']
    client.post(f'/api/versions/{old}/restore').raise_for_status()
    items = client.get(route,params={'cursor':cursor}).json()['items']
    assert [(i['actor'],i['action']) for i in items] == [('alice',a) for a in ('create','edit','update','recover')]

    cursor = client.get(route).json()['cursor']
    client.patch(f'/api/groups/{lid}/files', json={'path':'计划.txt','name':'改名.txt'}).raise_for_status()
    client.delete(f'/api/groups/{lid}/files', params={'path':'改名.txt'}).raise_for_status()
    items = client.get(route, params={'cursor':cursor}).json()['items']
    assert all(i['path'] == '' and i['action'] == 'refresh' for i in items) and len(items) == 2


def test_change_notifications_respect_paths_memberships_and_devices(env):
    alice, bob, outside = (env['clients'][n] for n in ('alice','bob','outside'))
    library = create_library(alice)
    alice.post(f'/api/groups/{library}/folders', json={'path':'可见'}).raise_for_status()
    share = alice.post(f'/api/libraries/{library}/shares',json={
        'path':'可见','subject_type':'user','subject_id':env['users']['bob']['id']})
    share.raise_for_status()
    route = '/api/file-changes'
    start = bob.get(route).json()
    assert start['items'] == []
    params = {'cursor':start['cursor']}
    upload(alice,library,'可见/方案.txt')
    upload(alice,library,'内部.txt')
    assert {i['path'] for i in alice.get(route,params=params).json()['items']} == {'可见/方案.txt','内部.txt'}
    items = bob.get(route,params=params).json()['items']
    assert [(i['actor'],i['path']) for i in items] == [('alice','可见/方案.txt')]
    assert not outside.get(route,params=params).json()['items']
    dav = TestClient(env['app'])
    assert dav.get('/dav/',params={**params,'file-changes':'1'},auth=env['auths']['bob']).json()['items'] == items
    mounted = next(root['name'] for root in env['store'].dav_roots(env['users']['bob']) if root['id'] == library)
    changed = dav.get('/dav/', params={**params,'file-changes':'1','refresh':'1'}, auth=env['auths']['bob']).json()
    assert changed['refresh_dirs'] == [mounted]
    assert changed['items'][0]['source'] == 'native' and changed['items'][0]['repo'] == library
    alice.delete('/api/shares/'+alice.get('/api/shares').json()[0]['id']).raise_for_status()
    assert not bob.get(route,params=params).json()['items']
    assert dav.get('/dav/', params={**params,'file-changes':'1','refresh':'1'},
                   auth=env['auths']['bob']).json()['refresh_dirs'] == []
    bob.delete('/api/tokens/'+env['auths']['bob'][1].split('.')[0]).raise_for_status()
    assert dav.get('/dav/?file-changes=1',auth=env['auths']['bob']).status_code == 401
