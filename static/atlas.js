/* Interactive geography. Coordinates are only drawn at their stated precision. */
(() => {
  const $ = id => document.getElementById(id);
  const state = { map:null, data:null, mode:'globe', walk:-1, active:null, isochrone:null, unverified:[] };
  let requestSequence=0;
  const emptyFC = () => ({type:'FeatureCollection', features:[]});
  const feature = (point, kind) => ({type:'Feature', geometry:{type:'Point',coordinates:[Number(point.lon),Number(point.lat)]}, properties:{id:point.id||'',name:point.name||point.title||'',kind}});
  const canAnimate = () => !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function say(id, message) { $(id).textContent = message; }
  function urlLink(label, url) { const a=document.createElement('a'); a.textContent=label; a.href=url; a.target='_blank'; a.rel='noopener noreferrer'; return a; }
  function placeholder(message) { const p=document.createElement('p'); p.className='hint'; p.textContent=message; return p; }
  function setMode(mode) {
    state.mode=mode;
    $('map-mode-globe').setAttribute('aria-pressed',String(mode==='globe'));
    $('map-mode-campus').setAttribute('aria-pressed',String(mode==='campus'));
    if (!state.map) return;
    state.map.setProjection({type:mode==='globe'?'globe':'mercator'});
    const c=state.data?.campuses?.[0];
    if(mode==='globe') state.map.flyTo({center:c?[c.lon,c.lat]:[45,25],zoom:c?1.85:1.25,duration:canAnimate()?1700:0});
    else if(c) state.map.flyTo({center:[c.lon,c.lat],zoom:c.precision==='city_centroid'?10:15.3,duration:canAnimate()?1600:0});
    else say('map-status','Сначала выберите университет.');
  }
  function layerData(kind) {
    const d=state.data||{};
    if(kind==='university') return (d.places||[]).map(p=>feature(p,kind));
    if(kind==='photos') return (d.photos||[]).map(p=>feature(p,kind));
    if(kind==='unverified') return state.unverified.map(p=>feature(p,kind));
    return (d[kind]||[]).filter(p=>p.lat!=null&&p.lon!=null).map(p=>feature(p,kind));
  }
  function updateSources() {
    if (!state.map?.getSource('ct-university')) return;
    for(const kind of ['university','buildings','dormitories','photos','unverified']) {
      state.map.getSource('ct-'+kind)?.setData({type:'FeatureCollection',features:layerData(kind)});
    }
    if(state.map.getSource('ct-isochrone')) state.map.getSource('ct-isochrone').setData(state.isochrone||emptyFC());
    state.map.getSource('ct-boundary')?.setData(state.data?.boundary||emptyFC());
    for(const [kind,source] of [['buildings','ct-building-shapes'],['dormitories','ct-dormitory-shapes']]) {
      const features=(state.data?.[kind]||[]).filter(p=>p.geometry).map(p=>({type:'Feature',geometry:p.geometry,properties:{id:p.id,name:p.name}}));
      state.map.getSource(source)?.setData({type:'FeatureCollection',features});
    }
    for(const [id,on] of [['ct-boundary-fill',$('layer-boundary').checked],['ct-boundary-line',$('layer-boundary').checked],['ct-building-shapes-layer',$('layer-buildings').checked],['ct-dormitory-shapes-layer',$('layer-dormitories').checked]]) {
      if(state.map.getLayer(id))state.map.setLayoutProperty(id,'visibility',on?'visible':'none');
    }
    for(const kind of ['buildings','dormitories','photos','unverified']) {
      const id='ct-'+kind;
      if(state.map.getLayer(id)) state.map.setLayoutProperty(id,'visibility',$('layer-'+kind).checked?'visible':'none');
    }
  }
  function addLayers() {
    const map=state.map;
    map.addSource('ct-boundary',{type:'geojson',data:emptyFC()});
    map.addLayer({id:'ct-boundary-fill',type:'fill',source:'ct-boundary',paint:{'fill-color':'#268453','fill-opacity':.12}});
    map.addLayer({id:'ct-boundary-line',type:'line',source:'ct-boundary',paint:{'line-color':'#137147','line-width':3,'line-dasharray':[3,2]}});
    map.on('click','ct-boundary-fill',()=>{const b=state.data?.boundary;if(!b)return;const box=$('place-evidence');box.replaceChildren();const h=document.createElement('h4');h.textContent='Граница кампуса';box.append(h,placeholder(b.properties.evidence),urlLink('Объект OpenStreetMap ↗',b.properties.source_url));});
    for(const [kind,id,color] of [['buildings','ct-building-shapes','#337a59'],['dormitories','ct-dormitory-shapes','#ba7346']]) {
      map.addSource(id,{type:'geojson',data:emptyFC()});
      map.addLayer({id:id+'-layer',type:'line',source:id,paint:{'line-color':color,'line-width':2}});
      map.on('click',id+'-layer',e=>showPlace(kind,e.features?.[0]?.properties?.id));
    }
    for(const kind of ['university','buildings','dormitories','photos','unverified']) {
      map.addSource('ct-'+kind,{type:'geojson',data:emptyFC()});
      map.addLayer({id:'ct-'+kind,type:'circle',source:'ct-'+kind,paint:{'circle-radius':kind==='university'?9:kind==='photos'?6:7,'circle-color':{university:'#205b39',buildings:'#317a55',dormitories:'#ba7346',photos:'#3f6cbb',unverified:'#9a8b6e'}[kind],'circle-stroke-color':'#fff','circle-stroke-width':2,'circle-opacity':.95}});
      map.on('click','ct-'+kind,e=>{ const id=e.features?.[0]?.properties?.id; showPlace(kind,id); });
      map.on('mouseenter','ct-'+kind,()=>map.getCanvas().style.cursor='pointer');
      map.on('mouseleave','ct-'+kind,()=>map.getCanvas().style.cursor='');
    }
    map.addSource('ct-isochrone',{type:'geojson',data:emptyFC()});
    map.addLayer({id:'ct-isochrone',type:'fill',source:'ct-isochrone',paint:{'fill-color':'#54aa7b','fill-opacity':.23}});
    map.addLayer({id:'ct-isochrone-line',type:'line',source:'ct-isochrone',paint:{'line-color':'#25734a','line-width':2}});
    updateSources();
  }
  function showPlace(kind,id) {
    const d=state.data||{};
    const pool=kind==='university'?d.places:kind==='photos'?d.photos:kind==='unverified'?state.unverified:d[kind];
    const p=(pool||[]).find(x=>x.id===id); if(!p)return;
    state.active=p;
    const holder=$('place-evidence'); holder.replaceChildren();
    const h=document.createElement('h4');h.textContent=p.name||p.title;holder.append(h);
    if(p.address)holder.append(placeholder(p.address));
    holder.append(placeholder(p.evidence||p.precision||'Координаты источника'));
    if(p.source_url)holder.append(urlLink('Первоисточник ↗',p.source_url));
    if(p.official_map)holder.append(document.createElement('br'),urlLink('Официальная карта ↗',p.official_map));
    if(p.license)holder.append(placeholder('Лицензия: '+p.license));
    if(p.author)holder.append(placeholder('Автор: '+p.author));
    if(p.captured_at)holder.append(placeholder('Дата съёмки: '+p.captured_at));
    if(p.image_url){const img=document.createElement('img');img.src=p.image_url;img.alt=p.title||p.name;img.loading='lazy';holder.append(img);}
    if(p.nearby_photos?.length){
      const lead=document.createElement('p');lead.className='hint';lead.textContent='Геокадры рядом с этим контуром. Близость координат не доказывает, что на фото именно этот корпус.';holder.append(lead);
      for(const item of p.nearby_photos){const photo=(state.data?.photos||[]).find(x=>x.id===item.id);if(!photo)continue;const b=document.createElement('button');b.type='button';b.className='nearby-photo';b.textContent=`Фото рядом, ${item.distance_m} м: ${photo.title}`;b.onclick=()=>showPlace('photos',photo.id);holder.append(b);}
    }
    if(state.map && p.lat!=null)state.map.flyTo({center:[p.lon,p.lat],zoom:16,duration:canAnimate()?950:0});
  }
  function walkPlaces() { return state.data?.walk_stops?.length ? state.data.walk_stops : (state.data?.places||[]).filter(p=>p.verified); }
  function renderWalk() {
    const places=walkPlaces();
    const box=$('walk-steps');box.replaceChildren();
    places.forEach((p,i)=>{const b=document.createElement('button');b.type='button';b.textContent=`${String(i+1).padStart(2,'0')} · ${p.name}`;b.setAttribute('aria-current',String(state.walk===i));b.onclick=()=>{state.walk=i;renderWalk();showPlace(state.data.walk_stops?.length?'buildings':'university',p.id);say('walk-status',`Остановка ${i+1} из ${places.length}: ${p.name}. Траектория по дорогам не рассчитана.`);$('walk-next').disabled=i>=places.length-1;};box.append(b);});
    $('walk-start').disabled=!places.length;
    if(!places.length)say('walk-status','Нет подтверждённых координат остановок. Прогулка не строится по догадкам.');
    else if(state.walk<0)say('walk-status',places.length===1?'Одна остановка с координатами. Откройте её.':`${places.length} остановок по карте OSM. Траектория по дорогам не рассчитана.`);
  }
  function resetScenario() {state.walk=-1;$('walk-next').disabled=true;$('walk-stop').disabled=true;renderWalk();state.isochrone=null;updateSources();$('reachability-details').replaceChildren();}
  async function load(ror) {
    const sequence=++requestSequence;
    say('map-status','Загружаю географию и её источники…');
    try {
      const response=await fetch('/api/atlas/'+ror); if(!response.ok)throw Error('HTTP '+response.status);
      const d=await response.json();if(sequence!==requestSequence)return;state.data=d;state.unverified=[];resetScenario();
      const c=d.campuses[0];
      $('map-location-title').textContent=c?`${d.institution} · ${c.name}`:`${d.institution} · координаты не найдены`;
      $('map-precision').textContent=c?.precision_text||'Координаты не найдены.';
      $('campus-select').replaceChildren();
      for(const camp of d.campuses){const o=document.createElement('option');o.value=camp.id;o.textContent=camp.name;$('campus-select').append(o);}
      $('campus-select').disabled=d.campuses.length<2;
      const google=$('google-maps-link');
      if(c){google.href='https://www.google.com/maps/search/?api=1&query='+encodeURIComponent(c.precision==='city_centroid'?d.institution+', '+(c.name||''):c.name+', '+(c.address||''));google.hidden=false;}
      else google.hidden=true;
      const notes=d.layer_notes;
      $('map-legend').textContent=`Граница: ${d.boundary?'OSM':'нет'} · Корпуса: ${d.buildings.length} · Общежития: ${d.dormitories.length} · Геокадры: ${d.photos.length}. ${notes.photos} ${d.attribution||''}`;
      for(const [kind, available] of [['boundary',Boolean(d.boundary)],['buildings',d.buildings.length>0],['dormitories',d.dormitories.length>0],['photos',d.photos.length>0]]) {
        $('layer-'+kind).disabled=!available;
        $('layer-'+kind).checked=available;
      }
      say('map-status',c?c.precision_text:'Карта показывает глобус; точная точка кампуса неизвестна.');
      say('reachability-status',c&&c.precision!=='city_centroid'?'Выберите режим и время, чтобы рассчитать зону по дорогам.':'Точная точка кампуса неизвестна: расчёт недоступен.');
      $('reachability-build').disabled=!c||c.precision==='city_centroid';
      updateSources();setMode('globe');
      say('map-status','Глобус показывает расположение университета. Приближаем кампус…');
      if (canAnimate()) window.setTimeout(() => { if (sequence === requestSequence) setMode('campus'); }, 1450);
      else setMode('campus');
      // The map now opens the profile, so loading it must not move the page.
    }catch(err){say('map-status','Не удалось загрузить географию: '+err.message);}
  }
  function setUnverified(candidate) {
    if(!candidate?.lat||!candidate?.lon)return;
    state.unverified=[{id:'nominatim-candidate',name:candidate.display_name||'Кандидат OSM',lat:Number(candidate.lat),lon:Number(candidate.lon),precision:'unverified_map_candidate',source_url:`https://www.openstreetmap.org/${candidate.osm_type}/${candidate.osm_id}`,evidence:'Кандидат Nominatim. Координата не подтверждена университетом.'}];
    updateSources();
    $('map-legend').textContent += ' · Непроверенный кандидат: 1';
  }
  function init() {
    $('map-mode-globe').onclick=()=>setMode('globe');$('map-mode-campus').onclick=()=>setMode('campus');
    for(const kind of ['boundary','buildings','dormitories','photos','unverified'])$('layer-'+kind).onchange=updateSources;
    $('walk-start').onclick=()=>{state.walk=0;$('walk-stop').disabled=false;renderWalk();const places=walkPlaces(),p=places[0];if(p){showPlace(state.data.walk_stops?.length?'buildings':'university',p.id);say('walk-status','Остановка 1: '+p.name+'. Траектория по дорогам не рассчитана.');$('walk-next').disabled=places.length<2;}};
    $('walk-next').onclick=()=>{const places=walkPlaces();if(state.walk+1<places.length){state.walk++;renderWalk();showPlace(state.data.walk_stops?.length?'buildings':'university',places[state.walk].id);say('walk-status',`Остановка ${state.walk+1} из ${places.length}: ${places[state.walk].name}. Траектория по дорогам не рассчитана.`);$('walk-next').disabled=state.walk>=places.length-1;}};
    $('walk-stop').onclick=()=>{state.walk=-1;$('walk-next').disabled=true;$('walk-stop').disabled=true;renderWalk();};
    $('reachability-build').onclick=async()=>{const c=state.data?.campuses?.[0];if(!c)return;say('reachability-status','Рассчитываю зону по дорожной сети…');try{const r=await fetch(`/api/atlas/${state.data.ror_id}/isochrone?mode=${$('reachability-mode').value}&minutes=${$('reachability-minutes').value}`);const d=await r.json();if(d.available){state.isochrone=d.geojson;updateSources();say('reachability-status',`Зона ${d.minutes} минут построена по дорогам.`);$('reachability-details').replaceChildren(urlLink('Расчёт: openrouteservice ↗',d.source));}else{state.isochrone=null;updateSources();say('reachability-status',d.reason);$('reachability-details').replaceChildren(placeholder(d.reason));}}catch(e){say('reachability-status','Ошибка расчёта: '+e.message);}};
    try {
      if(!window.maplibregl)throw Error('модуль карты не загружен');
      state.map=new maplibregl.Map({container:'atlas-map',style:'https://tiles.openfreemap.org/styles/liberty',center:[45,25],zoom:1.25,projection:{type:'globe'},attributionControl:true});
      state.map.addControl(new maplibregl.NavigationControl({showCompass:true}),'top-left');
      state.map.on('load',()=>{
        addLayers();
        if(state.data){
          setMode('globe');
          say('map-status','Глобус показывает расположение университета. Приближаем кампус…');
          if(canAnimate())window.setTimeout(()=>setMode('campus'),1450);else setMode('campus');
        } else say('map-status','Глобус готов. Выберите университет для перехода к кампусу.');
      });
      state.map.on('error',e=>{if(!state.map?.isStyleLoaded())say('map-status','Картографические тайлы недоступны. Поиск и ссылки на Google Maps работают.');});
    }catch(e){say('map-status','Карта недоступна: '+e.message);}
  }
  window.CampusAtlas={init,load,setUnverified,showPlace,resize:()=>state.map?.resize(),showPhoto:id=>{showPlace('photos',id);$('atlas-section').scrollIntoView({behavior:canAnimate()?'smooth':'instant'});}};
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
