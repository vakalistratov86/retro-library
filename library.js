'use strict';
// Library interaction layer: selection, metadata details and backup deletion.
let detailPath=null;
const metadataStates={queued:'В очереди оформления',processing:'Ищем информацию…',ready:'Оформлено',partial:'Сведения неполные',needs_review:'Уточните игру',not_found:'Совпадение не найдено',error:'Поиск недоступен'};
function visibleGames(){const q=$('search').value.toLowerCase();return state.games.filter(g=>(!selectedSystem||g.system===selectedSystem)&&(g.label+' '+(g.metadata?.title||'')).toLowerCase().includes(q))}
function publicSource(url){try{const u=new URL(url);return u.protocol==='https:'&&['github.com','raw.githubusercontent.com','ru.wikipedia.org','en.wikipedia.org','www.wikidata.org'].includes(u.hostname)?u.href:null}catch{return null}}
function renderGames(){
 const games=visibleGames();
 $('collection-title').textContent=selectedSystem||'Вся коллекция';$('result-count').textContent=games.length+' игр';
 $('selected-count').textContent='Для Apple TV выбрано '+state.games.filter(g=>g.selected).length+' из '+state.games.length;
 const incomplete=state.games.filter(g=>['year','genre','developer','publisher'].some(k=>!g.metadata?.[k])).length;
 $('metadata-status').textContent=state.metadata_current?'Оформляем игры · осталось в очереди: '+state.metadata_queue:incomplete?'Неполные сведения: '+incomplete+' игр · поиск будет повторён автоматически':'Сведения заполнены для всех игр · новые игры оформляются автоматически';
 $('games').innerHTML=games.length?games.map(g=>{
  const m=g.metadata||{}, facts=[m.year,m.genre].filter(Boolean).join(' · ');
  return '<article class="game '+(!g.selected?'game-unselected':'')+'"><button class="art game-open '+(g.cover?'':'placeholder')+'" data-path="'+escapeHTML(g.path)+'" aria-label="Информация: '+escapeHTML(g.label)+'">'+(g.cover?'<img loading="lazy" src="/cover/'+encodeURIComponent(g.cover)+'" alt="'+escapeHTML(g.label)+'">':escapeHTML(g.system))+'<span class="system-label">'+escapeHTML(g.system)+'</span></button><div class="game-body"><button class="game-title game-open" data-path="'+escapeHTML(g.path)+'">'+escapeHTML(g.label)+'</button><p class="game-facts">'+escapeHTML(facts||metadataStates[m.state]||'Ожидает оформления')+'</p><label class="sync-choice"><input type="checkbox" class="game-select" data-path="'+escapeHTML(g.path)+'" '+(g.selected?'checked':'')+'>На Apple TV</label><div class="game-bottom">'+(g.selected?pill(gameState(g)):'<span class="pill">Только NAS</span>')+(!g.cover?'<button class="cover-button" data-path="'+escapeHTML(g.path)+'" title="Добавить обложку PNG" aria-label="Добавить обложку">▧</button>':'')+'</div></div></article>';
 }).join(''):'<p class="empty">Игры не найдены. Добавьте ROM или измените поиск.</p>';
 document.querySelectorAll('.game-open').forEach(b=>b.onclick=()=>{detailPath=b.dataset.path;renderGameDetails();$('game-dialog').showModal()});
 document.querySelectorAll('.game-select').forEach(input=>input.onchange=async()=>{input.disabled=true;try{await post('/api/selection',{paths:[input.dataset.path],selected:input.checked});await refresh()}catch(e){toast(e.message);input.checked=!input.checked;input.disabled=false}});
 document.querySelectorAll('.cover-button').forEach(b=>b.onclick=()=>{coverGame=state.games.find(g=>g.path===b.dataset.path);$('cover-input').click()});
 if($('game-dialog').open)renderGameDetails();
}
function renderGameDetails(){
 const g=state.games.find(x=>x.path===detailPath);if(!g)return;
 const m=g.metadata||{};$('game-detail-title').textContent=g.label;
 $('game-detail-status').textContent=(metadataStates[m.state]||'Ожидает оформления')+(m.match?' · '+({crc32:'совпадение CRC32',title:'по названию',manual:'выбрано вручную',verified:'проверенная запись',wikidata:'по названию и платформе'}[m.match]||''):'');
 $('game-detail-cover').innerHTML=g.cover?'<img src="/cover/'+encodeURIComponent(g.cover)+'" alt="'+escapeHTML(g.label)+'">':'<span>'+escapeHTML(g.system)+'</span>';
 const facts=[['Название в каталоге',m.title],['Система',g.system],['Год',m.year],['Жанр',m.genre],['Разработчик',m.developer],['Издатель',m.publisher]];
 $('game-detail-facts').innerHTML=facts.map(([key,value])=>'<div><dt>'+key+'</dt><dd>'+escapeHTML(value||'Нет данных')+'</dd></div>').join('');
 const description=m.description;
 $('game-description').textContent=description?.text||'Описание не найдено. Подтверждённые сведения и ссылки на источники приведены в карточке.';
 const wiki=publicSource(description?.url);$('game-attribution').innerHTML=wiki?'<a href="'+escapeHTML(wiki)+'" target="_blank" rel="noopener noreferrer">Wikipedia — источник описания</a> · CC BY-SA 4.0 · текст может быть сокращён':'';
 $('game-sources').innerHTML=[...new Set([...(m.sources||[]),...Object.values(m.artwork_sources||{})])].filter(publicSource).map((url,i)=>'<a href="'+escapeHTML(url)+'" target="_blank" rel="noopener noreferrer">Источник '+(i+1)+'</a>').join(' · ');
 const fieldNames={year:'год',genre:'жанр',developer:'разработчик',publisher:'издатель'};
 const missing=(m.missing_fields||Object.keys(fieldNames).filter(k=>!m[k])).map(k=>fieldNames[k]).join(', ');
 const retry=m.next_retry?' Следующий поиск: '+new Date(m.next_retry*1000).toLocaleString('ru-RU')+'.':'';
 $('game-metadata-error').textContent=(m.error||(missing?'Не найдены: '+missing+'.':(m.warnings||[]).length?'Часть источников временно недоступна.':''))+retry;
 const candidate=$('metadata-candidate'), old=candidate.value;candidate.innerHTML=(m.candidates||[]).map(c=>'<option value="'+escapeHTML(c.title)+'">'+escapeHTML(c.title)+(c.year?' · '+escapeHTML(c.year):'')+'</option>').join('');if([...candidate.options].some(x=>x.value===old))candidate.value=old;
 $('candidate-section').hidden=!(m.candidates||[]).length;$('metadata-retry').disabled=['queued','processing'].includes(m.state);
}
$('metadata-retry').onclick=async()=>{try{await post('/api/metadata',{path:detailPath});toast('Поиск информации запущен');await refresh()}catch(e){toast(e.message)}};
$('metadata-choose').onclick=async()=>{try{await post('/api/metadata',{path:detailPath,chosen:$('metadata-candidate').value});toast('Выбранная игра поставлена в очередь оформления');await refresh()}catch(e){toast(e.message)}};
for(const [id,selected] of [['select-visible',true],['deselect-visible',false]])$(id).onclick=async()=>{const paths=visibleGames().map(g=>g.path);if(!paths.length)return;try{await post('/api/selection',{paths,selected});toast(selected?'Игры выбраны для Apple TV':'Игры исключены из синхронизации');await refresh()}catch(e){toast(e.message)}};
let deleteBackupName=null;
function attachBackupDeleteButtons(){
 document.querySelectorAll('.backup-record').forEach((row,index)=>{
  if(row.querySelector('.backup-delete'))return;
  const record=state.backups[index],button=document.createElement('button');button.type='button';button.className='secondary backup-delete';button.textContent='Удалить';button.onclick=()=>{deleteBackupName=record.name;$('delete-backup-name').textContent=record.name;$('delete-backup-error').textContent='';$('delete-backup-dialog').showModal()};row.querySelector('.backup-actions').append(button);
 });
}
const baseRenderBackups=renderBackups;
renderBackups=function(){baseRenderBackups();attachBackupDeleteButtons()};
$('search').oninput=renderGames;
$('delete-backup-confirm').onclick=async()=>{try{$('delete-backup-confirm').disabled=true;await post('/api/backup/delete',{name:deleteBackupName});$('delete-backup-dialog').close();toast('Резервная копия удалена');await refresh()}catch(e){$('delete-backup-error').textContent=e.message}finally{$('delete-backup-confirm').disabled=false}};
