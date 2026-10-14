/* Equal-participant conversations. Existing chat state stays in app.js. */
(() => {
  const el = id => document.getElementById(id);
  const workbench = document.querySelector('.workbench');
  const states = {idle:'Готова к разговору',running:'Обсуждение идёт',paused:'Пауза',stopped:'Остановлено'};
  const memberStates = {waiting:'ожидает',queued:'в очереди',responding:'отвечает',pass:'пропускает ход',unavailable:'недоступен',waiting_external:'ждём ответ MCP',left:'вышел'};
  const desktopStates = {idle:'готов к запуску',queued:'ждёт запуска окна',opening:'запускается',sent:'команда отправлена',connected:'подключён',failed:'ошибка запуска'};
  const reasons = {manual:'',restart:'Приложение было перезапущено. Можно продолжить.',all_pass:'Участникам нечего добавить. Новое сообщение продолжит разговор.',time_limit:'Время истекло.',turn_limit:'Достигнут лимит ходов.',participants:'Нужно минимум два участника.',participants_changed:'Состав участников изменён. Можно продолжить с оставшимися.',storage_or_dispatch_error:'Ошибка обсуждения. История сохранена.',desktop_launch_failed:'Не удалось запустить участника. Подробности — в его статусе.'};
  let selectedRoom = null, room = null, source = null, requestVersion = 0, remainingAt = 0, roomList = [], loading = false, hasOlder = false;
  const sending = new Set();
  const messages = new Map(), draftMessages = new Map();
  async function request(path, method='GET', body) {
    const response = await fetch(managerURL('/api/rooms'+path),{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
    const value = await response.json();
    if (!response.ok) throw Error(value.error || 'Не удалось выполнить действие');
    return value;
  }
  function notify(error) { el('rooms-notice').textContent = error?.message || error || ''; }
  function button(label, fn, css='quiet') {const b=document.createElement('button');b.type='button';b.className=css;b.textContent=label;b.onclick=fn;return b;}
  function setRoom(value) {
    room=value; remainingAt=Date.now();
    for (const message of value.messages || []) messages.set(message.id,message);
    render();
  }
  async function list() {
    roomList=(await request('')).rooms;
    const target=el('room-list');target.replaceChildren();
    for(const entry of roomList){const b=button(entry.title,()=>select(entry.id));b.setAttribute('aria-current',String(entry.id===selectedRoom));target.append(b);}
    if(!roomList.length){const p=document.createElement('p');p.className='caption';p.textContent='Пока нет комнат';target.append(p);}
  }
  async function select(id) {
    if(selectedRoom)draftMessages.set(selectedRoom,el('room-input').value);
    selectedRoom=id;room=null;messages.clear();source?.close();source=null;
    const version=++requestVersion;
    el('room-input').value=draftMessages.get(id)||'';
    el('room-invite').hidden=true;el('room-invite-command').value='';
    el('room-messages').textContent='Загружаем переписку…';
    try {
      const value=await request('/'+id);
      if(version!==requestVersion)return;
      hasOlder=value.messages.length===100;
      setRoom(value);await list();
      document.body.classList.remove('rooms-nav-open');el('rooms-list-toggle').setAttribute('aria-expanded','false');
      source=new EventSource(managerURL('/api/rooms/'+id+'/events?after='+value.event_cursor));
      source.addEventListener('room',event=>{
        if(selectedRoom!==id)return;
        const change=JSON.parse(event.data);
        if(change.id<=room.event_cursor)return;
        room.event_cursor=change.id;
        if(change.kind==='message')messages.set(change.value.id,change.value);
        else if(change.kind==='state'){Object.assign(room,change.value);if(change.value.settings)room.limits=JSON.parse(change.value.settings);remainingAt=Date.now();}
        else if(change.kind==='member'&&!room.members.some(p=>p.id===change.value.id))room.members.push(change.value);
        else if(change.kind==='member_state'||change.kind==='membership'){
          const member=room.members.find(p=>p.id===change.value.id);if(member)Object.assign(member,change.value);
        }
        else if(change.kind==='desktop_state'){
          const member=room.members.find(p=>p.id===change.value.id);if(member)Object.assign(member.config,change.value);
        }
        render();
      });
      source.addEventListener('reset',event=>{if(selectedRoom===id){const value=JSON.parse(event.data);hasOlder=value.messages.length===100;messages.clear();setRoom(value);}});
      source.onopen=()=>notify('');source.onerror=()=>notify('Связь прервана. Восстанавливаем; переписка сохраняется на сервере.');
      el('room-messages').scrollTop=el('room-messages').scrollHeight;
    } catch(error){notify(error);}
  }
  function render() {
    if(!room)return;
    el('room-title').textContent=room.title;
    el('room-state').textContent=(states[room.status]||room.status)+(reasons[room.reason]?' · '+reasons[room.reason]:'');
    const live=room.status==='running', enabled=room.members.filter(p=>p.enabled);
    el('room-start').disabled=live||enabled.length<2;el('room-pause').disabled=!live;el('room-stop').disabled=!live;
    el('room-start').textContent=enabled.some(p=>p.config?.desktop_client)?'Запустить участие':'Обсудить';
    el('room-resume').disabled=live||!['paused','stopped'].includes(room.status)||enabled.length<2;
    el('room-settings-open').disabled=false;el('room-input').disabled=false;el('room-send').disabled=sending.has(selectedRoom);
    el('room-export').hidden=false;el('room-export').href=managerURL('/api/rooms/'+room.id+'/export');
    const target=el('room-members');target.replaceChildren();
    for(const member of enabled){const badge=document.createElement('span');badge.className='room-member';badge.dataset.state=member.config?.desktop_state==='failed'?'unavailable':member.state;
      const phase=member.config?.desktop_state;
      badge.textContent=member.name+(member.config?.desktop_model?' · '+member.config.desktop_model:'')+' · '+(phase&&phase!=='connected'?(desktopStates[phase]||phase):(memberStates[member.state]||member.state));
      badge.title=member.config?.desktop_detail||'';target.append(badge);}
    renderMessages();timer();
  }
  function renderMessages() {
    const target=el('room-messages'), bottom=target.scrollHeight-target.scrollTop-target.clientHeight<100;
    const entries=[...messages.values()].sort((a,b)=>a.id-b.id);
    target.querySelector('.empty')?.remove();
    if(!entries.length){target.textContent='';const empty=document.createElement('div');empty.className='empty';empty.textContent='Напишите первое сообщение, добавьте участников и нажмите «Обсудить».';target.append(empty);return;}
    // Keyed rows preserve selection and reading position during token updates.
    if(target.firstChild?.nodeType===Node.TEXT_NODE)target.replaceChildren();
    let older=target.querySelector('[data-room-older]');
    if(!older){older=button('Показать более ранние сообщения',async()=>{
      const id=selectedRoom,first=Math.min(...messages.keys()),oldHeight=target.scrollHeight;
      try{const value=await request('/'+id+'/messages?before='+first);if(selectedRoom!==id)return;hasOlder=value.messages.length===100;for(const m of value.messages)messages.set(m.id,m);renderMessages();target.scrollTop+=target.scrollHeight-oldHeight;}catch(error){notify(error);}
    });older.dataset.roomOlder='true';target.prepend(older);}
    older.hidden=!hasOlder;
    let previous=older;
    for(const message of entries){
      let row=target.querySelector('[data-room-message="'+message.id+'"]');
      if(!row){
        row=document.createElement('article');row.className='message'+(message.author_id==='user'?' room-user':'');row.dataset.roomMessage=message.id;
        const header=document.createElement('div');header.className='message-header';const speaker=document.createElement('span');speaker.className='speaker';speaker.textContent=message.author;
        const state=document.createElement('span');state.className='caption room-message-state';header.append(speaker,state);
        const body=document.createElement('div');body.className='message-body';
        header.append(button('Копировать',async()=>{try{await navigator.clipboard.writeText(messages.get(message.id)?.content||'');notify('Реплика скопирована');}catch(error){notify('Не удалось скопировать реплику');}},'quiet copy-answer'));
        row.append(header,body);
      }
      const body=row.querySelector('.message-body'),display=message.content.trimStart();if(body.textContent!==display){
        body.textContent=display;
        if(message.author_id==='web'){
          body.replaceChildren();const lines=display.split('\n');
          lines.forEach((line,index)=>{let node=document.createTextNode(line);
            try{const url=new URL(line);if(['http:','https:'].includes(url.protocol)&&!url.username&&!url.password){node=document.createElement('a');node.href=url.href;node.textContent=line;node.target='_blank';node.rel='noopener noreferrer';}}catch(_){}
            body.append(node);if(index<lines.length-1)body.append(document.createTextNode('\n'));
          });
        }
      }
      row.querySelector('.room-message-state').textContent={streaming:'пишет…',interrupted:'прервано · сохранено',error:'ошибка · текст сохранён',done:message.content.trim()==='PASS'?'пропустил ход':''}[message.state]||'';
      if(previous.nextElementSibling!==row)target.insertBefore(row,previous.nextElementSibling);previous=row;
    }
    if(bottom)target.scrollTop=target.scrollHeight;
    el('room-latest').hidden=bottom;
  }
  function timer(){if(!room)return;const seconds=Math.max(0,Math.ceil(room.remaining-(room.status==='running'?(Date.now()-remainingAt)/1000:0)));el('room-time').textContent=Math.floor(seconds/60)+':'+String(seconds%60).padStart(2,'0')+' · '+room.turns+'/'+room.limits.max_turns+' ходов';}
  async function control(action){try{setRoom(await request('/'+selectedRoom+'/control','POST',{action}));await list();}catch(error){notify(error);}}
  el('rooms-open').onclick=async()=>{
    workbench.hidden=true;el('rooms-workbench').hidden=false;document.body.classList.add('rooms-mode');
    try{await list();if(!selectedRoom&&roomList.length)await select(roomList[0].id);}catch(error){notify(error);}
  };
  el('rooms-back').onclick=()=>{workbench.hidden=false;el('rooms-workbench').hidden=true;document.body.classList.remove('rooms-mode');};
  el('rooms-list-toggle').onclick=()=>{const open=document.body.classList.toggle('rooms-nav-open');el('rooms-list-toggle').setAttribute('aria-expanded',String(open));};
  el('room-new').onclick=()=>{el('room-create').showModal();loadCodexModels('room-create-codex-model');};el('room-create-close').onclick=()=>el('room-create').close();
  el('room-create-form').onsubmit=async event=>{event.preventDefault();if(loading)return;loading=true;
    try{const value=await request('','POST',{title:el('room-create-title').value,topic:el('room-create-topic').value||el('room-create-title').value,web_search:el('room-create-web').checked});
      if(el('room-create-participants').value==='desktop'){
        for(const client of ['codex','gemini'])await request('/'+value.id+'/members','POST',{name:client==='codex'?'Codex':'Gemini',kind:'mcp',desktop_client:client,codex_model:client==='codex'?el('room-create-codex-model').value:undefined});
      }
      el('room-create').close();event.target.reset();await select(value.id);
    }catch(error){notify(error);}finally{loading=false;}
  };
  el('room-compose').onsubmit=async event=>{event.preventDefault();const id=selectedRoom,content=el('room-input').value;if(!id||!content.trim()||sending.has(id))return;sending.add(id);el('room-send').disabled=true;
    try{const value=await request('/'+id+'/messages','POST',{content});if(draftMessages.get(id)===content)draftMessages.delete(id);if(selectedRoom===id){if(el('room-input').value===content)el('room-input').value='';messages.set(value.id,value);renderMessages();el('room-messages').scrollTop=el('room-messages').scrollHeight;}}catch(error){notify(error);}finally{sending.delete(id);if(selectedRoom===id)el('room-send').disabled=false;}
  };
  for(const action of ['start','pause','stop','resume'])el('room-'+action).onclick=()=>control(action);
  el('room-latest').onclick=()=>{el('room-messages').scrollTop=el('room-messages').scrollHeight;el('room-latest').hidden=true;};
  el('room-settings-close').onclick=()=>el('room-settings').close();
  async function loadCodexModels(id){
    const target=el(id);const previous=target.value;target.disabled=true;
    try{const value=await request('/codex-models');target.replaceChildren();
      for(const model of value.models){const option=document.createElement('option');option.value=model.id;option.textContent=model.name;target.append(option);}
      target.value=value.models.some(m=>m.id===previous)?previous:value.default;target.disabled=false;
    }catch(error){target.replaceChildren();const option=document.createElement('option');option.value='';option.textContent='Codex недоступен';target.append(option);notify(error);}
  }
  function adapterFields(){const kind=el('room-member-kind').value;el('room-codex-model-label').hidden=kind!=='codex';if(kind==='codex')loadCodexModels('room-codex-model');el('room-endpoint-fields').hidden=kind!=='openai';el('room-remote-label').hidden=kind!=='openai';el('room-remote').checked=false;
    el('room-adapter-help').textContent={managed:'Используется уже загруженная модель УЗЛА. Второй тяжёлый процесс не запускается. Несколько участников могут использовать одну модель.',openai:'Явное подключение к работающему сервису. Для внешнего HTTPS требуется ваше разрешение.',mcp:'После добавления появится ключ и команда локального MCP-сервера. Агент читает свой ожидающий ход и публикует ответ.',codex:'УЗЕЛ создаст сеанс через Codex App Server и сразу отправит команду участия. Используется вход в Codex. Модель выбирается ниже; окно приложения не требуется.',gemini:'УЗЕЛ откроет отдельный проект Antigravity в Gemini и отправит команду участия. Модель наследуется из клиента.'}[kind];
  }
  el('room-member-kind').onchange=adapterFields;adapterFields();
  function renderMemberSettings(){const target=el('room-member-settings');target.replaceChildren();for(const member of room.members.filter(p=>p.enabled)){const row=document.createElement('div'),label=document.createElement('span');label.textContent=member.name+' · '+(member.config?.desktop_client==='codex'?'Codex App Server · '+(member.config.codex_model||member.config.desktop_model||'модель по умолчанию'):member.kind);row.append(label,button('Исключить',async()=>{try{setRoom(await request('/'+selectedRoom+'/remove-member','POST',{id:member.id}));renderMemberSettings();}catch(error){el('room-settings-notice').textContent=error.message;}}));target.append(row);}}
  el('room-settings-open').onclick=async()=>{if(!room)return;renderMemberSettings();const duration=room.limits.duration;el('room-duration').value=[300,900,1800].includes(duration)?String(duration):'custom';el('room-custom-label').hidden=el('room-duration').value!=='custom';el('room-custom-minutes').value=duration/60;el('room-max-turns').value=room.limits.max_turns;el('room-empty-turns').value=room.limits.empty_turns;el('room-web').checked=Boolean(room.web_search);el('room-settings-notice').textContent='';el('room-settings').showModal();
  };
  el('room-member-form').onsubmit=async event=>{event.preventDefault();const kind=el('room-member-kind').value,id=selectedRoom;
    try{const desktop=['codex','gemini'].includes(kind);const result=await request('/'+id+'/members','POST',{name:el('room-member-name').value,kind:desktop?'mcp':kind,desktop_client:desktop?kind:undefined,codex_model:kind==='codex'?el('room-codex-model').value:undefined,endpoint:el('room-endpoint').value,model:el('room-model').value,allow_remote:el('room-remote').checked});if(selectedRoom!==id)return;setRoom(await request('/'+id));renderMemberSettings();el('room-member-name').value='';
      if(result.invite_token&&!desktop){el('room-invite').hidden=false;el('room-invite-command').value=result.mcp_command;el('room-settings-notice').textContent='Подключите эту команду в клиенте MCP. УЗЕЛ должен оставаться запущенным.';}
      if(desktop)el('room-settings-notice').textContent='Участник добавлен. Кнопка «Запустить участие» создаст сеанс клиента и отправит команду участия.';
    }catch(error){el('room-settings-notice').textContent=error.message;}
  };
  el('room-duration').onchange=()=>{el('room-custom-label').hidden=el('room-duration').value!=='custom';};
  el('room-limits-form').onsubmit=async event=>{event.preventDefault();try{const duration=el('room-duration').value==='custom'?Number(el('room-custom-minutes').value)*60:Number(el('room-duration').value);setRoom(await request('/'+selectedRoom+'/settings','POST',{limits:{...room.limits,duration,max_turns:Number(el('room-max-turns').value),empty_turns:Number(el('room-empty-turns').value)},web_search:el('room-web').checked}));el('room-settings-notice').textContent='Настройки сохранены';}catch(error){el('room-settings-notice').textContent=error.message;}};
  el('room-invite-copy').onclick=async()=>{try{await navigator.clipboard.writeText(el('room-invite-command').value);el('room-settings-notice').textContent='Команда скопирована';}catch(error){el('room-settings-notice').textContent='Не удалось скопировать. Выделите команду вручную.';}};
  window.addEventListener('beforeunload',()=>source?.close());setInterval(timer,1000);
})();
