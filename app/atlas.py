"""Geography with explicit precision and source per point."""
from __future__ import annotations
import os
import json
from pathlib import Path
import httpx
from typing import Any
from math import asin, cos, radians, sin, sqrt
from . import db

POINTS = {
 '052bx8q98': {'name':'Главный кампус NU','lat':51.09,'lon':71.399444,'precision':'institution_point','source_url':'https://www.wikidata.org/wiki/Q2783344','source_label':'Wikidata: координата университета','official_map':'https://campussecurity.nu.edu.kz/campusmap','address':'53 Kabanbay Batyr Avenue, Astana','address_source':'https://cf.nu.edu.kz/contact'},
 '05jnvbc31': {'name':'Astana IT University, EXPO','lat':51.0908607,'lon':71.4184149,'precision':'osm_named_point','source_url':'https://www.openstreetmap.org/node/10575843769','source_label':'OpenStreetMap: точка с названием Astana IT University, блок C1','official_map':'https://astanait.edu.kz/ru/contacts','address':'55/11 Mangilik El Avenue, EXPO Business Center, C1','address_source':'https://astanait.edu.kz/ru/contacts'},
 '00b30xv10': {'name':'University of Pennsylvania','lat':39.951944,'lon':-75.193611,'precision':'institution_point','source_url':'https://www.wikidata.org/wiki/Q49117','source_label':'Wikidata: координата университета','official_map':'https://facilities.upenn.edu/maps/printable-maps','address':'Philadelphia, Pennsylvania','address_source':'https://facilities.upenn.edu/maps/locations'},
}
PRECISION_TEXT = {'institution_point':'Точка университета по Wikidata; не граница участка и не координата каждого корпуса.','osm_named_point':'Именованная точка OSM рядом с официальным адресом; не граница здания и не подтверждённый вход.','city_centroid':'Центр города из ROR/GeoNames; расположение кампуса неизвестно.'}


async def enrich_osm(atlas: dict[str,Any], institution: dict[str,Any]) -> dict[str,Any]:
    """Buildings inside an OSM university area matched by exact Wikidata ID."""
    qid = institution.get('wikidata_id')
    if atlas.get('boundary') or not qid or not qid.startswith('Q') or not qid[1:].isdigit(): return atlas
    key = 'osm:v1:'+qid
    mapped = db.get_cached(key)
    if mapped is None:
        query = f'''[out:json][timeout:8];
        nwr["amenity"="university"]["wikidata"="{qid}"]->.campus;
        .campus out geom; .campus map_to_area->.a;
        way(area.a)["building"]; out geom 100;'''
        try:
            async with httpx.AsyncClient(timeout=11) as client:
                response = await client.post('https://overpass-api.de/api/interpreter',data={'data':query})
                response.raise_for_status()
                elements = response.json().get('elements',[])
            mapped = {'buildings':[], 'dormitories':[], 'boundary':None}
            for element in elements:
                tags = element.get('tags',{}); points = element.get('geometry',[])
                if len(points) < 4 or points[0] != points[-1]: continue
                ring = [[p['lon'],p['lat']] for p in points]
                geometry = {'type':'Polygon','coordinates':[ring]}
                source = f"https://www.openstreetmap.org/{element['type']}/{element['id']}"
                if tags.get('wikidata') == qid and tags.get('amenity') == 'university':
                    mapped['boundary'] = {'type':'Feature','geometry':geometry,'properties':{'source_url':source}}
                elif tags.get('building'):
                    dorm = tags['building'] in ('dormitory','residential') and ('dorm' in tags.get('name','').lower() or tags['building']=='dormitory')
                    kind = 'dormitories' if dorm else 'buildings'
                    mapped[kind].append({'id':f"osm-{element['id']}", 'name':tags.get('name') or ('Общежитие OSM' if dorm else 'Корпус OSM'), 'lat':sum(p[1] for p in ring[:-1])/(len(ring)-1), 'lon':sum(p[0] for p in ring[:-1])/(len(ring)-1), 'geometry':geometry, 'source_url':source, 'evidence':'OSM: здание внутри территории университета, сопоставленной по Wikidata', 'precision':'osm_building', 'verified':False})
            db.set_cached(key,mapped,86400 if elements else 600)
        except (httpx.HTTPError,ValueError,KeyError):
            atlas['scope_note'] = 'Сервис границ OSM временно недоступен. Показаны доступные координаты и фото.'
            return atlas
    if mapped.get('boundary') or mapped.get('buildings'):
        atlas.update(mapped)
        atlas['attribution'] = '© OpenStreetMap contributors'
        atlas['scope_note'] = 'OSM: территория сопоставлена по Wikidata; назначение каждого корпуса требует проверки.'
    return atlas

def build_atlas(institution: dict[str,Any], assets: list[dict[str,Any]]) -> dict[str,Any]:
    ror = institution['ror_id']; seed = POINTS.get(ror)
    if seed: campus = {**seed, 'id':ror+'-main'}
    elif institution.get('campus_coordinates'):
        point = institution['campus_coordinates']
        campus = {'id':ror+'-main','name':institution['name'],'lat':point['lat'],'lon':point['lon'],'precision':'institution_point','source_url':point['source'],'source_label':'Wikidata: координата университета','official_map':None,'address':None,'address_source':None}
    else:
        city = institution.get('city_coordinates')
        campus = {'id':ror+'-city','name':institution.get('city') or 'Город','lat':city['lat'],'lon':city['lon'],'precision':'city_centroid','source_url':'https://ror.org/'+ror,'source_label':'ROR / GeoNames','official_map':None,'address':None,'address_source':None} if city else None
    if campus: campus['precision_text'] = PRECISION_TEXT[campus['precision']]
    places = []
    if campus and campus['precision'] != 'city_centroid':
        places.append({'id':campus['id'],'name':campus['name'],'kind':'university','lat':campus['lat'],'lon':campus['lon'],'precision':campus['precision'],'source_url':campus['source_url'],'evidence':campus['source_label'],'verified':campus['precision'] in ('institution_point','osm_named_point'),'address':campus['address'],'official_map':campus['official_map']})
    geotagged = []
    for asset in assets:
        coord = asset.get('coordinates')
        if isinstance(coord, dict) and isinstance(coord.get('lat'),(float,int)) and isinstance(coord.get('lon'),(float,int)):
            geotagged.append({'id':asset['id'],'title':asset['title'],'lat':coord['lat'],'lon':coord['lon'],
                             'source_url':asset['source_url'],'license':asset.get('license'),'precision':'image_geotag',
                             'image_url':asset.get('image_url'),'author':asset.get('author'),'captured_at':asset.get('captured_at'),
                             'evidence':'Геотег страницы Commons: '+('место камеры' if coord.get('type')=='camera' else 'место объекта' if coord.get('type')=='object' else 'тип координаты не уточнён')})
    mapped = {'boundary':None,'buildings':[],'dormitories':[],'walk_stops':[],'attribution':None}
    map_files = {'052bx8q98':'atlas_nu.json','00b30xv10':'atlas_upenn.json'}
    if ror in map_files:
        mapped = json.loads((Path(__file__).resolve().parents[1] / 'data' / map_files[ror]).read_text())

    # A camera location near a mapped footprint is useful navigation context. It
    # remains a proximity relationship, never proof that the building is depicted.
    for building in mapped["buildings"] + mapped["dormitories"]:
        nearby = []
        for photo in geotagged:
            lat1, lon1, lat2, lon2 = map(radians, (building["lat"], building["lon"], photo["lat"], photo["lon"]))
            metres = 6_371_000 * 2 * asin(sqrt(sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2))
            if metres <= 90:
                nearby.append({"id": photo["id"], "distance_m": round(metres)})
        if nearby:
            building["nearby_photos"] = nearby
    return {'institution':institution['name'],'ror_id':ror,'campuses':[campus] if campus else [],'places':places,
            'boundary':mapped['boundary'],'buildings':mapped['buildings'],'dormitories':mapped['dormitories'],
            'walk_stops':mapped['walk_stops'],'attribution':mapped['attribution'],'scope_note':mapped.get('scope_note'),
            'photos':geotagged,'unverified':[],
            'layer_notes':{'boundary':'Граница OSM, сопоставленная по названию, сайту и Wikidata; не официальный кадастровый план.' if mapped['boundary'] else 'Геометрия границы пока отсутствует.',
                           'buildings':'OSM-корпуса внутри границы, назначение требует проверки по официальной карте.',
                           'dormitories':'OSM-общежития внутри границы, назначение требует проверки.',
                           'photos':'Показаны лишь кадры с собственными геотегами; положение в категории не считается геотегом.'}}

async def isochrone(lat: float, lon: float, mode: str, minutes: int) -> dict[str,Any]:
    key = os.getenv('OPENROUTESERVICE_API_KEY','')
    if not key:
        return {'available':False,'reason':'Для расчёта по дорожной сети нужен OPENROUTESERVICE_API_KEY. Зона не подменяется кругом по прямой.'}
    if mode not in ('walking','cycling') or minutes not in (10,15,30):
        return {'available':False,'reason':'Недопустимый режим или время.'}
    profile = {'walking':'foot-walking','cycling':'cycling-regular'}[mode]
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            response = await client.post('https://api.openrouteservice.org/v2/isochrones/'+profile,headers={'Authorization':key,'Content-Type':'application/json','Accept':'application/json'},json={'locations':[[lon,lat]],'range':[minutes*60],'range_type':'time','attributes':['area','reachfactor']})
            response.raise_for_status(); data=response.json()
            return {'available':True,'geojson':data,'source':'https://openrouteservice.org/dev/','mode':mode,'minutes':minutes}
        except (httpx.HTTPError,ValueError) as exc:
            return {'available':False,'reason':f'Сервис маршрутизации временно недоступен: {type(exc).__name__}'}
