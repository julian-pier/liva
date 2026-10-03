(function () {
  var isMobile = window.LIVA && window.LIVA.isMobile
    ? window.LIVA.isMobile()
    : window.matchMedia("(max-width: 900px)").matches;
  if (isMobile) return;
  var root = document.getElementById("endurance-planner");
  if (!root) return;
  var DAY = 86400000;
  var state = { plans: [], plan: null, start: monday(new Date()), selectedId: null, inspectorOpen: false, inspectorTab: "overview", calendarView: "grid", metric: "pace", loadMetric: "distance", busy: false, syncBusy: false };
  function esc(v) { return String(v == null ? "" : v).replace(/[&<>"']/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }
  function iso(d) { return new Date(d.getTime() - d.getTimezoneOffset()*60000).toISOString().slice(0,10); }
  function monday(d) { var x=new Date(d); x.setHours(12,0,0,0); x.setDate(x.getDate()-((x.getDay()+6)%7)); return x; }
  function addDays(d,n){ var x=new Date(d); x.setDate(x.getDate()+n); return x; }
  function asDate(value){ return new Date(value+(String(value).length===10?"T12:00:00":"")); }
  function dayCount(start,end){ return Math.round((asDate(end)-asDate(start))/DAY)+1; }
  function clamp(value,min,max){ return Math.min(max,Math.max(min,value)); }
  function shortDate(value){ return new Intl.DateTimeFormat("de-DE",{day:"numeric",month:"short"}).format(asDate(value)); }
  function pace(sec){ var total=Math.round(Number(sec)||0);return total ? Math.floor(total/60)+":"+String(total%60).padStart(2,"0")+" / km" : "—"; }
  function paceRange(target){var fast=target.pace_min_s_per_km,slow=target.pace_max_s_per_km;if(!fast||!slow)return pace(target.pace_s_per_km);return pace(fast).replace(" / km","")+"–"+pace(slow);}
  function clock(sec){ var total=Math.round(Number(sec)||0);return total ? Math.floor(total/60)+":"+String(total%60).padStart(2,"0") : "—"; }
  function month(s){ return s ? new Intl.DateTimeFormat("de-DE",{month:"short",year:"numeric"}).format(new Date(s+"T12:00:00")) : "—"; }
  async function api(path, options) {
    var response = await fetch(path, Object.assign({headers:{"Content-Type":"application/json"}}, options || {}));
    var data = await response.json();
    if (!response.ok || data.ok === false) throw new Error((data.error && (data.error.message || data.error.code)) || "Aktion fehlgeschlagen");
    return data;
  }
  function toast(message, error) {
    var el=document.querySelector(".ep-toast");
    if(!el){el=document.createElement("div");el.className="ep-toast";document.body.appendChild(el);}
    el.textContent=message;
    el.style.cssText="position:fixed;right:18px;bottom:18px;z-index:120;padding:10px 14px;border-radius:7px;background:"+(error?"#7b2d35":"#245b49")+";color:white;font:600 12px system-ui";
    clearTimeout(el._timer); el._timer=setTimeout(function(){el.remove();},2600);
  }
  async function loadPlans(preferred) {
    try {
      var data=await api("/api/endurance/plans");
      state.plans=data.plans||[];
      var id=preferred||(state.plan&&state.plan.id)||(state.plans[0]&&state.plans[0].id);
      if(id){
        state.plan=(await api("/api/endurance/plans/"+id)).plan;
        state.selectedId=state.selectedId||(state.plan.sessions[0]&&state.plan.sessions[0].id)||null;
        var first=(state.plan.sessions[0]&&state.plan.sessions[0].scheduled_date)||(state.plan.phases[0]&&state.plan.phases[0].start_date);
        if(first&&!preferred)state.start=monday(new Date(first+"T12:00:00"));
      } else state.plan=null;
      render();
    } catch(e) {
      root.innerHTML='<div class="ep-empty"><div><h2>Cardio-Planung nicht verfügbar</h2><p>'+esc(e.message)+'</p><button class="ep-btn" data-action="reload">Neu laden</button></div></div>';
      bind();
    }
  }
  function renderEmpty(){
    root.innerHTML='<div class="ep-empty"><div><div class="ep-empty-mark">⌁</div><h2>Noch kein Cardio-Plan</h2><p>Ziel setzen und den Plan hier oder über GPT aufbauen.</p><button class="ep-btn primary" data-action="create-plan">Ziel setzen</button></div></div>';
    bind();
  }
  function goalHtml(e){
    var title=e.distance_m ? Math.round(e.distance_m)+" m" : (e.title||"Ziel");
    return '<section class="ep-goal"><div class="ep-run-mark" aria-hidden="true">↗</div><div class="ep-goal-main"><div class="ep-kicker">Hauptziel</div><div class="ep-goal-title">'+esc(title)+' · Ziel '+clock(e.target_time_s)+' · '+month(e.event_date)+'</div></div><div class="ep-goal-pace"><span class="ep-kicker">Zielpace</span><strong>'+pace(e.goal_pace_s_per_km)+'</strong></div></section>';
  }
  function pageHeaderHtml(p,e,options){
    var distance=e.distance_m?Math.round(e.distance_m/100)/10+" km":"Ausdauerziel",eventDate=e.event_date?new Intl.DateTimeFormat("de-DE",{day:"numeric",month:"long",year:"numeric"}).format(asDate(e.event_date)):"Termin offen";
    var subtitle=distance+(e.target_time_s?" in "+clock(e.target_time_s):"")+" · "+eventDate;
    var sync=p.sync||{},syncText=state.syncBusy?'Intervals · synchronisiert …':sync.errors?'Intervals ⚠ '+sync.errors:sync.pending?'Intervals · '+sync.pending+' warten':'Intervals ✓';
    var syncTitle=state.syncBusy?'Synchronisierung läuft':sync.errors?sync.errors+' Synchronisierungsfehler – klicken zum erneuten Starten':sync.pending?sync.pending+' Änderungen warten – jetzt synchronisieren':'Alles synchronisiert – klicken, um erneut zu prüfen';
    return '<header class="ep-page-header"><div class="ep-header-copy"><span class="ep-header-eyebrow">Planung</span><h1>'+esc(p.title||"Cardio")+'</h1><p>'+esc(subtitle)+'</p></div><div class="ep-header-actions"><div class="ep-header-action-row"><button type="button" data-action="sync-now" class="ep-sync-pill '+(state.syncBusy?'is-running':sync.errors?'is-error':sync.pending?'is-pending':'is-ok')+'" title="'+esc(syncTitle)+'" '+(state.syncBusy?'disabled aria-busy="true"':'')+'>'+esc(syncText)+'</button><label class="ep-plan-picker"><span>Plan</span><select class="ep-plan-select" aria-label="Cardio-Plan">'+options+'</select></label><button class="ep-btn" data-action="edit-goal">Ziel bearbeiten</button><button class="ep-icon-btn" aria-label="Weitere Aktionen">•••</button></div><div class="ep-header-stats"><span><small>Zielpace</small><b>'+pace(e.goal_pace_s_per_km)+'</b></span><span><small>Ziel</small><b>'+esc(eventDate.replace(/\s\d{4}$/,""))+'</b></span></div></div></header>';
  }
  function timelineHtml(p){
    if(!p.phases.length)return '<div class="ep-timeline ep-timeline-empty">Noch keine Saisonphasen</div>';
    var phases=p.phases.slice().sort(function(a,b){return (a.sort_order-b.sort_order)||a.start_date.localeCompare(b.start_date);});
    var start=asDate(phases[0].start_date), goal=asDate((p.event&&p.event.event_date)||phases[phases.length-1].end_date), total=Math.max(1,dayCount(iso(start),iso(goal)));
    var today=asDate(iso(new Date())), todayOffset=Math.round((today-start)/DAY), markerLeft=clamp(todayOffset/Math.max(1,total-1)*100,0,100);
    var active=phases.find(function(ph){return today>=asDate(ph.start_date)&&today<=asDate(ph.end_date);});
    var currentText=today<start?"Heute · noch "+Math.ceil((start-today)/DAY)+" Tage bis Start":today>goal?"Heute · Ziel abgeschlossen":active?"Heute · "+active.name+" · Tag "+(Math.round((today-asDate(active.start_date))/DAY)+1)+" / "+dayCount(active.start_date,active.end_date):"Heute";
    var phaseHtml=phases.map(function(ph,index){
      var days=dayCount(ph.start_date,ph.end_date), weeks=Math.round(days/7*10)/10;
      return '<div class="ep-phase '+(active&&active.id===ph.id?'is-current':'')+'" data-phase="'+ph.id+'" data-index="'+index+'" data-kind="'+ph.phase_type+'" style="flex-basis:'+(days/total*100)+'%"><strong>'+esc(ph.name)+'</strong><small data-phase-duration>'+weeks.toLocaleString("de-DE")+' Wochen</small><span class="ep-phase-dates" data-phase-dates>'+shortDate(ph.start_date)+' – '+shortDate(ph.end_date)+'</span>'+(index<phases.length-1?'<i class="ep-phase-handle end" data-resize="end" aria-label="Grenze zu '+esc(phases[index+1].name)+' verschieben"></i>':'')+'</div>';
    }).join("");
    var ticks=[],cursor=new Date(start.getFullYear(),start.getMonth(),1,12);if(cursor<start)cursor=new Date(start.getFullYear(),start.getMonth()+1,1,12);
    while(cursor<goal){ticks.push('<span style="left:'+((cursor-start)/DAY/Math.max(1,total-1)*100)+'%">'+new Intl.DateTimeFormat("de-DE",{month:"short"}).format(cursor)+'</span>');cursor=new Date(cursor.getFullYear(),cursor.getMonth()+1,1,12);}
    return '<div class="ep-season-status"><span class="ep-status-dot"></span><strong>'+esc(currentText)+'</strong><span>Saison · '+total+' Tage</span></div><div class="ep-timeline-wrap"><div class="ep-month-scale">'+ticks.join("")+'</div><div class="ep-timeline" data-total-days="'+total+'">'+phaseHtml+'<div class="ep-today-marker '+(today<start?'is-before':today>goal?'is-after':'')+'" style="left:'+markerLeft+'%"><span>Heute</span></div><div class="ep-race" aria-label="Ziel"><b>⚑</b></div></div><div class="ep-season-ends"><span>Start · '+shortDate(iso(start))+'</span><span>Ziel · '+shortDate(iso(goal))+'</span></div></div>';
  }
  function loadHtml(rows){
    var metric=state.loadMetric;
    var values=(rows||[]).map(function(r){return metric==="distance"?r.distance_m/1000:metric==="duration"?r.duration_s/3600:(r.distance_m>0?r.duration_s/(r.distance_m/1000):0);});
    var positive=values.filter(function(v){return v>0;}).sort(function(a,b){return a-b;});
    if(!positive.length)return '<div class="ep-load-chart is-empty" data-chart><div><b>Keine '+({distance:"Distanz",duration:"Dauer",pace:"Ø Pace"}[metric])+' erfasst</b><small>Für diese Ansicht fehlen noch geplante Werte.</small></div></div>';
    var cap=positive[Math.min(positive.length-1,Math.floor(positive.length*.9))]||1,min=positive[0],max=positive[positive.length-1],unit=metric==="distance"?" km":metric==="duration"?" h":" / km";
    function display(value){return metric==="pace"?pace(value):Math.round(value*10)/10+unit;}
    var bars=values.map(function(v,i){
      var h=0;if(v){h=metric==="pace"?(max===min?52:Math.max(12,Math.min(76,12+(max-v)/(max-min)*64))):Math.max(7,Math.min(76,v/cap*76));}
      var row=rows[i],weekEnd=iso(addDays(asDate(row.week_start),6)),avgPace=row.distance_m>0?row.duration_s/(row.distance_m/1000):0;
      var tooltip=shortDate(row.week_start)+" – "+shortDate(weekEnd)+" · "+Math.round((row.distance_m||0)/100)/10+" km · "+Math.round((row.duration_s||0)/60)+" min · Ø "+pace(avgPace);
      var edge=i<4?" tip-left":i>rows.length-5?" tip-right":"";
      return '<span class="ep-load-column'+edge+'" tabindex="0" data-tip="'+esc(tooltip)+'"><i class="ep-load-bar" style="--h:'+h+'px" aria-label="'+esc(tooltip)+'"></i>'+(i%4===0?'<small>'+shortDate(row.week_start)+'</small>':'')+'</span>';
    }).join("");
    var top=metric==="pace"?display(min):display(cap),bottom=metric==="pace"?display(max):"0";
    return '<div class="ep-load-chart is-'+metric+'" data-chart><div class="ep-chart-axis"><span>'+top+'</span><span>'+bottom+'</span></div>'+bars+'</div>';
  }
  function calendarGridHtml(p){
    var names=["Mo","Di","Mi","Do","Fr","Sa","So"], out='<div class="ep-calendar">';
    for(var w=0;w<4;w++){
      out+='<div class="ep-week">';
      for(var d=0;d<7;d++){
        var day=addDays(state.start,w*7+d), key=iso(day);
        var sessions=p.sessions.filter(function(s){return s.scheduled_date===key;});
        var gym=(p.gym_context&&(p.gym_context[key]||p.gym_context[names[d]]))||[];
        out+='<div class="ep-day '+(key===iso(new Date())?"is-today":"")+'" data-date="'+key+'"><div class="ep-date"><strong>'+names[d]+'</strong> '+day.getDate()+'.'+(day.getMonth()+1)+'.</div>';
        out+=sessions.map(function(s){return '<button class="ep-session '+(s.id===state.selectedId?"is-selected":"")+'" draggable="true" data-session="'+s.id+'" data-type="'+s.session_type+'"><b>'+esc(s.title||s.session_type.toUpperCase())+'</b><span>'+(s.duration_s?Math.round(s.duration_s/60)+" min":s.distance_m?"~ "+(s.distance_m/1000).toFixed(1)+" km":"")+'</span></button>';}).join("");
        out+=gym.map(function(g){return '<div class="ep-gym">▣ '+esc(g.title)+'</div>';}).join("");
        out+='<button class="ep-icon-btn" data-add-date="'+key+'" aria-label="Einheit hinzufügen" style="width:24px;height:22px;min-height:22px;border:0;background:transparent">+</button></div>';
      }
      out+='</div>';
    }
    return out+'</div>';
  }
  function calendarListHtml(p){
    var out='<div class="ep-calendar-list">',formatter=new Intl.DateTimeFormat("de-DE",{weekday:"short",day:"2-digit",month:"short"});
    for(var i=0;i<28;i++){
      var day=addDays(state.start,i),key=iso(day),sessions=p.sessions.filter(function(s){return s.scheduled_date===key;}),gym=(p.gym_context&&p.gym_context[key])||[];
      if(!sessions.length&&!gym.length)continue;
      out+='<section class="ep-list-day '+(key===iso(new Date())?'is-today':'')+'"><div class="ep-list-date"><b>'+formatter.format(day)+'</b><button class="ep-icon-btn" data-add-date="'+key+'" aria-label="Einheit hinzufügen">+</button></div><div>';
      out+=sessions.map(function(s){return '<button class="ep-list-session '+(s.id===state.selectedId?'is-selected':'')+'" data-session="'+s.id+'" data-type="'+s.session_type+'"><span><b>'+esc(s.title||tagLabel(s.session_type))+'</b><small>'+esc(tagLabel(s.session_type))+' · '+(s.duration_s?Math.round(s.duration_s/60)+' min':s.distance_m?(s.distance_m/1000).toFixed(1)+' km':'offen')+'</small></span><span>Details ›</span></button>';}).join("");
      out+=gym.map(function(g){return '<div class="ep-list-gym"><span>Gym</span><b>'+esc(g.title)+'</b><small>Kontext · schreibgeschützt</small></div>';}).join("");
      out+='</div></section>';
    }
    return out+(out==='<div class="ep-calendar-list">'?'<div class="ep-list-empty">In diesen vier Wochen ist noch nichts geplant.</div>':'')+'</div>';
  }
  function calendarHtml(p){return state.calendarView==="list"?calendarListHtml(p):calendarGridHtml(p);}
  function warningsHtml(items){
    if(!items||!items.length)return "";
    return '<div class="ep-warning-list">'+items.map(function(x){return '<div class="ep-warning">⚠ '+esc(x.message)+'</div>';}).join("")+'</div>';
  }
  function kindLabel(value){return ({open:"Locker",warmup:"Einlaufen",work:"Belastung",stride:"Steigerung",recovery:"Pause",cooldown:"Auslaufen",repeat:"Wiederholungen"})[value]||value;}
  function tagLabel(value){return ({easy:"Locker",steady:"Zügig",long:"Langer Lauf",threshold:"Schwelle",interval:"Intervalle",vo2:"VO₂max","5k_specific":"5-km-spezifisch",race:"Wettkampf",morning:"morgens",quality:"Qualität"})[value]||value;}
  function statusLabel(value){return ({planned:"Geplant",completed:"Abgeschlossen",skipped:"Ausgelassen",cancelled:"Abgesagt"})[value]||value||"Geplant";}
  function friendlyChange(value){var text=String(value||""),undo=text.match(/^Rückgängig:\s*(.+)$/i),redo=text.match(/^Wiederholt:\s*(.+)$/i),raw=(undo&&undo[1])||(redo&&redo[1])||text,map={move_session:"Einheit verschoben",resize_phase:"Phase angepasst",move_phase:"Phase verschoben",derive_metrics:"Zielbereiche aktualisiert",update_session:"Einheit aktualisiert",replace_steps:"Ablauf aktualisiert",set_session_target:"Zielbereich aktualisiert",set_goal_event:"Wettkampfziel aktualisiert",normalize_phases:"Saisonplan geordnet"},label=map[raw]||raw.replace(/_/g," ");return undo?"Rückgängig: "+label:redo?"Wiederholt: "+label:label;}
  function changeStamp(value){var created=new Date(value),today=new Date(),same=created.getFullYear()===today.getFullYear()&&created.getMonth()===today.getMonth()&&created.getDate()===today.getDate();return new Intl.DateTimeFormat("de-DE",same?{hour:"2-digit",minute:"2-digit"}:{day:"2-digit",month:"2-digit"}).format(created);}
  function changesHtml(items){
    var rows=(items||[]).slice(0,5).map(function(x){return '<div class="ep-change"><time>'+changeStamp(x.created_at)+'</time><span>'+esc(friendlyChange(x.summary))+'</span></div>';}).join("");
    return '<div class="ep-changes"><div class="ep-section-head"><h2>Letzte Änderungen</h2></div>'+(rows||'<div class="ep-change"><span></span><span>Noch keine Änderungen</span></div>')+'</div>';
  }
  function flatSteps(steps){
    var by={},out=[];(steps||[]).forEach(function(s){var k=s.parent_step_id||"";(by[k]||(by[k]=[])).push(s);});
    function walk(parent,depth){(by[parent]||[]).sort(function(a,b){return a.sort_order-b.sort_order;}).forEach(function(s){out.push(Object.assign({depth:depth},s));walk(s.id,depth+1);});}
    walk("",0);return out;
  }
  function executionRows(session){return Array.isArray(session&&session.execution_steps)?session.execution_steps:[];}
  function durationLabel(seconds){var total=Math.round(Number(seconds)||0);if(!total)return "";if(total<60)return total+" s";return total%60?Math.floor(total/60)+":"+String(total%60).padStart(2,"0")+" min":total/60+" min";}
  function hrLabel(target,resolved){if(target.hr_bpm)return Math.round(target.hr_bpm)+" bpm";var maxHr=Number((resolved||{}).estimated_max_hr_bpm),lo=target.hr_min_pct!=null?target.hr_min_pct:(resolved||{}).internal_hr_min_pct,hi=target.hr_max_pct!=null?target.hr_max_pct:(resolved||{}).internal_hr_max_pct;if(maxHr>0&&lo!=null){hi=hi==null?lo:hi;return Math.round(maxHr*Number(lo)/100)+"–"+Math.round(maxHr*Number(hi)/100)+" bpm";}return "";}
  function rpeLabel(target){var lo=target.rpe_min!=null?target.rpe_min:(target.metric==="rpe"?target.min:target.rpe),hi=target.rpe_max!=null?target.rpe_max:(target.metric==="rpe"?target.max:null);return lo!=null?"RPE "+lo+(hi!=null&&hi!==lo?"–"+hi:""):"";}
  function metricDatum(s,metric,maxHr){var target=s.target||{},resolved=s.resolved||{};if(metric==="pace"){var t=resolved.pace_s_per_km||resolved.pace_min_s_per_km?resolved:target;if(t.pace_s_per_km||t.pace_min_s_per_km){var fast=Number(t.pace_min_s_per_km||t.pace_s_per_km),slow=Number(t.pace_max_s_per_km||t.pace_s_per_km);return {value:Number(t.pace_s_per_km||((fast+slow)/2)),min:fast,max:slow,label:paceRange(t)};}}if(metric==="hr"){if(target.hr_bpm){var bpm=Number(target.hr_bpm);return {value:bpm,min:bpm,max:bpm,label:Math.round(bpm)+" bpm"};}var hrMax=Number(maxHr||resolved.estimated_max_hr_bpm),lo=target.hr_min_pct!=null?target.hr_min_pct:resolved.internal_hr_min_pct,hi=target.hr_max_pct!=null?target.hr_max_pct:resolved.internal_hr_max_pct;if(hrMax>0&&lo!=null){var lowBpm=Math.round(hrMax*Number(lo)/100),highBpm=Math.round(hrMax*Number(hi==null?lo:hi)/100);return {value:(lowBpm+highBpm)/2,min:lowBpm,max:highBpm,label:lowBpm+"–"+highBpm+" bpm"};}}if(metric==="rpe"){var rlo=target.rpe_min!=null?target.rpe_min:(target.metric==="rpe"?target.min:target.rpe),rhi=target.rpe_max!=null?target.rpe_max:(target.metric==="rpe"?target.max:rlo);if(rlo!=null)return {value:(Number(rlo)+Number(rhi==null?rlo:rhi))/2,min:Number(rlo),max:Number(rhi==null?rlo:rhi),label:"RPE "+rlo+(rhi!=null&&rhi!==rlo?"–"+rhi:"")};}return null;}
  function preferredMetric(){return "pace";}
  function stepAmount(s){return s.duration_s?durationLabel(s.duration_s):s.distance_m?(Number(s.distance_m)>=1000?(Number(s.distance_m)/1000).toLocaleString("de-DE")+" km":Math.round(s.distance_m)+" m"):"";}
  function internalLabel(s){var target=s.target||{},parts=[],heart=hrLabel(target,s.resolved),effort=rpeLabel(target);if(heart)parts.push(heart);if(effort)parts.push(effort);return parts.join(" · ");}
  function repetitionLabel(s){return s.repeat_index?"Wiederholung "+s.repeat_index+"/"+s.repeat_count:"";}
  function workoutSummary(rows){
    var lead=rows.find(function(s){return s.repeat_group_id&&s.kind==="work";})||rows.find(function(s){return s.repeat_group_id&&s.kind==="stride";});
    if(!lead)return rows.length===1?kindLabel(rows[0].kind)+" · "+stepAmount(rows[0]):rows.length+" aufeinander abgestimmte Schritte";
    var count=Number(lead.repeat_count)||rows.filter(function(s){return s.repeat_group_id===lead.repeat_group_id&&s.kind===lead.kind;}).length;
    return count+" × "+stepAmount(lead)+(lead.kind==="stride"?" Steigerungen":"");
  }
  function workoutChart(session){
    var rows=executionRows(session),metric=state.metric||"pace";
    if(!rows.length)return '<div class="ep-workout-empty">Kein ausführbarer Ablauf vorhanden.</div>';
    var available=window.LivaWorkoutCharts&&window.LivaWorkoutCharts.buildProfile(rows,metric).points.length;
    if(!available)return '<div class="ep-workout-empty">Für '+(metric==="hr"?"Puls":metric==="rpe"?"RPE":"Pace")+' fehlt noch ein ausführbarer Zielwert.</div>';
    var label=metric==="hr"?"Puls":metric==="rpe"?"Belastungsgefühl":"Pace";
    return '<figure class="ep-workout-route is-'+metric+'" aria-label="'+label+'-Verlauf dieser Einheit"><figcaption><span>'+label+'</span><b>'+esc(workoutSummary(rows))+'</b></figcaption><div class="ep-workout-canvas-wrap"><canvas data-planned-workout-chart></canvas></div><div class="ep-route-legend"><span data-kind="warmup">Einlaufen</span><span data-kind="stride">Steigerung</span><span data-kind="work">Belastung</span><span data-kind="recovery">Pause</span><span data-kind="cooldown">Auslaufen</span></div></figure>';
  }
  function anchorHtml(a){
    if(!a)return '<div class="ep-anchor"><h4>Leistungsstand</h4><p class="ep-kicker">Noch kein aktueller 5-km-Test hinterlegt.</p><button class="ep-btn" data-action="set-anchor">Leistung eintragen</button></div>';
    return '<div class="ep-anchor"><h4>Leistungsstand</h4><div class="ep-anchor-grid"><div><small>Aktualisiert</small><strong>'+esc(a.anchor_date)+'</strong></div><div><small>Quelle</small><strong>'+esc(a.source)+'</strong></div><div><small>5 km aktuell</small><strong>'+clock(a.five_k_time_s)+'</strong></div><div><small>Schwelle</small><strong>'+pace(a.threshold_pace_s_per_km)+'</strong></div></div><div class="ep-actions"><button class="ep-btn" data-action="set-anchor">Ändern</button><button class="ep-btn" data-action="rebase">Künftige Ziele aktualisieren</button></div></div>';
  }
  function inspectorHtml(s){
    if(!s)return '<div class="ep-empty" style="min-height:400px"><div><p>Einheit auswählen</p><button class="ep-btn" data-action="add-session">+ Einheit</button></div></div>';
    var rows=executionRows(s),steps=rows.map(function(st){var paceData=metricDatum(st,"pace"),internal=internalLabel(st),note=String(st.notes||"").trim();return '<div class="ep-execution-step" data-kind="'+esc(st.kind)+'"><span class="ep-step-index">'+String(st.execution_index||"").padStart(2,"0")+'</span><div class="ep-step-copy"><div><b>'+esc(kindLabel(st.kind))+'</b>'+(st.repeat_index?'<em>'+esc(repetitionLabel(st))+'</em>':'')+'</div>'+(note?'<p>'+esc(note)+'</p>':'')+'<small>'+esc(internal||"Nach Gefühl")+'</small></div><div class="ep-step-target"><strong>'+esc(stepAmount(st))+'</strong><span>'+(paceData?esc(paceData.label):"offen")+'</span></div></div>';}).join("");
    var tagValues=Array.from(new Set([s.session_type].concat(s.tags||[]))),tags=tagValues.map(function(t){return '<span class="ep-tag">'+esc(tagLabel(t))+'</span>';}).join(""),contract=s.execution_contract||{},contractText=contract.step_count?contract.step_count+" Schritte geprüft":"Ablauf wird geprüft";
    var coachNote=s.notes?'<p class="ep-coach-note">'+esc(s.notes)+'</p>':'';
    var metricSwitch='<div class="ep-workout-metrics" role="group" aria-label="Diagrammwert"><button data-metric="pace" class="'+(state.metric==='pace'?'is-active':'')+'">Pace</button><button data-metric="hr" class="'+(state.metric==='hr'?'is-active':'')+'">Puls</button><button data-metric="rpe" class="'+(state.metric==='rpe'?'is-active':'')+'">RPE</button></div>';
    var overview=coachNote+metricSwitch+workoutChart(s)+'<div class="ep-session-facts"><div><small>Zeit</small><b>'+(s.duration_s?durationLabel(s.duration_s):'—')+'</b></div><div><small>Strecke</small><b>'+(s.distance_m?(s.distance_m/1000).toFixed(1).replace(".",",")+' km':'—')+'</b></div><div><small>Ablauf</small><b>'+esc(workoutSummary(rows))+'</b></div></div><div class="ep-intent-line"><span>Planung: Puls & RPE</span><b>Trainingsuhr: Pace</b></div>';
    var syncInfo=s.intervals_sync||{},syncState=syncInfo.sync_state||s.sync_state,syncLabel=syncState==="synced"?'● Synchronisiert':syncState==="sync_error"?'● Fehler':syncState==="dirty"?'● Änderung wartet':'● Wartet auf Synchronisierung';
    var details='<div class="ep-contract-state"><span class="ep-sync-state is-'+esc(syncState)+'">'+esc(syncLabel)+'</span><b>'+esc(contractText)+'</b><small>LIVA → Intervals → Garmin</small></div><div class="ep-execution-list">'+(steps||'<div class="ep-workout-empty">Noch keine Schritte</div>')+'</div>'+(syncState==="sync_error"?'<p class="ep-sync-error">'+esc(syncInfo.last_error||'Synchronisierung fehlgeschlagen')+'</p><button class="ep-btn" data-action="retry-sync">Erneut versuchen</button>':'');
    var adjustments='<div class="ep-adjustments"><p class="ep-kicker">Änderungen gelten nur für diese Einheit. Neue Leistungswerte verändern künftige Ziele erst nach deiner Bestätigung.</p><button class="ep-btn" data-action="edit-steps">Schritte bearbeiten</button>'+anchorHtml(state.plan.fitness_anchor)+'<div class="ep-actions"><button class="ep-btn" data-action="move-selected">Verschieben</button><button class="ep-btn" data-action="duplicate">Duplizieren</button><button class="ep-btn danger" data-action="delete-session">Löschen</button></div></div>';
    var content=state.inspectorTab==="details"?details:state.inspectorTab==="adjustments"?adjustments:overview;
    var dateLabel=new Intl.DateTimeFormat("de-DE",{weekday:"long",day:"numeric",month:"long"}).format(new Date(s.scheduled_date+"T12:00:00"));
    return '<div class="ep-inspector-head"><span class="ep-session-date">'+esc(dateLabel)+'</span><button class="ep-icon-btn ep-inspector-close" data-action="close-inspector" aria-label="Schließen">×</button></div><h3>'+esc(s.title)+'</h3><div class="ep-tags">'+tags+'</div><div class="ep-tabs" role="tablist"><button role="tab" aria-selected="'+(state.inspectorTab==='overview')+'" class="'+(state.inspectorTab==='overview'?'is-active':'')+'" data-inspector-tab="overview">Training</button><button role="tab" aria-selected="'+(state.inspectorTab==='details')+'" class="'+(state.inspectorTab==='details'?'is-active':'')+'" data-inspector-tab="details">Schritte</button><button role="tab" aria-selected="'+(state.inspectorTab==='adjustments')+'" class="'+(state.inspectorTab==='adjustments'?'is-active':'')+'" data-inspector-tab="adjustments">Optionen</button></div><div class="ep-tab-panel" role="tabpanel">'+content+'</div>';
  }
  function render(){
    if(!state.plan){renderEmpty();return;}
    var p=state.plan,e=p.event||{},selected=p.sessions.find(function(s){return s.id===state.selectedId;})||p.sessions[0];state.selectedId=selected&&selected.id;
    var options=state.plans.map(function(x){return '<option value="'+x.id+'" '+(x.id===p.id?"selected":"")+'>'+esc(x.title)+'</option>';}).join("");
    var loadButtons=["distance","duration","pace"].map(function(m){return '<button data-load="'+m+'" class="'+(state.loadMetric===m?"is-active":"")+'">'+({distance:"Distanz",duration:"Dauer",pace:"Ø Pace"}[m])+'</button>';}).join("");
    var end=addDays(state.start,27), period=new Intl.DateTimeFormat("de-DE",{day:"numeric",month:"short"}).format(state.start)+" – "+new Intl.DateTimeFormat("de-DE",{day:"numeric",month:"short",year:"numeric"}).format(end);
    root.innerHTML=pageHeaderHtml(p,e,options)+'<section class="ep-overview"><div class="ep-section-head"><h2>Saisonüberblick</h2><button class="ep-btn" data-action="add-phase">+ Phase</button></div>'+timelineHtml(p)+'<div class="ep-section-head ep-load-head"><h2>Wochenbelastung</h2><div class="ep-view-switch">'+loadButtons+'</div></div>'+loadHtml(p.load_summary)+'</section><div class="ep-workspace"><main class="ep-calendar-area"><div class="ep-calendar-nav"><h2>Trainingsplan</h2><button class="ep-icon-btn" data-action="prev">‹</button><button class="ep-btn" data-action="today">Heute</button><button class="ep-icon-btn" data-action="next">›</button><span class="ep-period">'+period+'</span><div class="ep-view-switch"><button data-calendar-view="grid" aria-pressed="'+(state.calendarView==='grid')+'" class="'+(state.calendarView==='grid'?'is-active':'')+'">4 Wochen</button><button data-calendar-view="list" aria-pressed="'+(state.calendarView==='list')+'" class="'+(state.calendarView==='list'?'is-active':'')+'">Liste</button></div></div>'+calendarHtml(p)+warningsHtml(p.warnings)+changesHtml(p.recent_changes)+'</main><aside class="ep-inspector '+(state.inspectorOpen?"is-open":"")+'">'+inspectorHtml(selected)+'</aside></div>';
    var historyControls=document.createElement("div");historyControls.className="ep-history-controls";historyControls.innerHTML='<button type="button" class="ep-icon-btn" data-action="undo" title="Letzte Änderung rückgängig (Strg+Z)" aria-label="Letzte Änderung rückgängig" aria-keyshortcuts="Control+Z Meta+Z">↶</button><button type="button" class="ep-icon-btn" data-action="redo" title="Letzte Änderung wiederholen (Strg+Y)" aria-label="Letzte Änderung wiederholen" aria-keyshortcuts="Control+Y Meta+Y">↷</button>';var historyButtons=historyControls.querySelectorAll("button");historyButtons[0].disabled=!p.undo_available;historyButtons[1].disabled=!p.redo_available;var picker=root.querySelector(".ep-plan-picker");picker.parentNode.insertBefore(historyControls,picker.nextSibling);
    bind();
    var canvas=root.querySelector("[data-planned-workout-chart]");
    if(canvas&&selected&&window.LivaWorkoutCharts)window.LivaWorkoutCharts.mount(canvas,executionRows(selected),state.metric||"pace");
  }
  function field(name,label,type,value,extra){
    return '<label style="font-size:11px;color:#9aa7b3">'+label+'<input name="'+name+'" type="'+(type||"text")+'" value="'+esc(value||"")+'" '+(extra||"")+' style="display:block;width:100%;margin-top:5px;background:#0d141a;color:white;border:1px solid #34414d;border-radius:5px;padding:9px;font-size:16px"></label>';
  }
  function modal(title, body, save){
    var old=document.querySelector(".ep-modal");if(old)old.remove();
    var el=document.createElement("div");el.className="ep-modal";el.innerHTML='<form class="ep-modal-card"><h3>'+esc(title)+'</h3><div class="ep-form">'+body+'</div><div class="ep-actions" style="justify-content:flex-end"><button type="button" class="ep-btn" data-cancel>Abbrechen</button><button class="ep-btn primary">'+esc(save||"Speichern")+'</button></div></form>';document.body.appendChild(el);
    el.querySelector("[data-cancel]").onclick=function(){el.remove();};el.onclick=function(e){if(e.target===el)el.remove();};return el;
  }
  async function patch(operations, options){
    if(state.busy)return;state.busy=true;options=options||{};
    try{
      var data=await api("/api/endurance/plans/"+state.plan.id,{method:"PATCH",body:JSON.stringify({operations:operations,dry_run:!!options.dryRun,confirm:!!options.confirm,expected_revision:state.plan.revision})});
      if(options.dryRun)return data;state.plan=data.plan;render();return data;
    } finally { state.busy=false; }
  }
  async function undo(){
    if(state.busy)return;
    if(!state.plan||!state.plan.undo_available){toast("Keine Änderung zum Rückgängigmachen");return;}
    state.busy=true;
    try{
      var data=await api("/api/endurance/plans/"+state.plan.id+"/undo",{method:"POST",body:JSON.stringify({expected_revision:state.plan.revision})});
      if(data.plan_removed){state.plan=null;state.selectedId=null;await loadPlans();}else{state.plan=data.plan;render();}toast("Rückgängig: "+(data.summary||"letzte Änderung"));
    }catch(e){
      if(e.message==="revision_conflict")await loadPlans(state.plan.id);
      toast(e.message==="revision_conflict"?"Plan wurde zwischenzeitlich geändert – neu geladen":e.message,true);
    }finally{state.busy=false;}
  }
  async function redo(){
    if(state.busy)return;
    if(!state.plan||!state.plan.redo_available){toast("Keine Änderung zum Wiederholen");return;}
    state.busy=true;
    try{
      var data=await api("/api/endurance/plans/"+state.plan.id+"/redo",{method:"POST",body:JSON.stringify({expected_revision:state.plan.revision})});
      state.plan=data.plan;render();toast("Wiederholt: "+(data.summary||"letzte Änderung"));
    }catch(e){
      if(e.message==="revision_conflict")await loadPlans(state.plan.id);
      toast(e.message==="revision_conflict"?"Plan wurde zwischenzeitlich geändert – neu geladen":e.message,true);
    }finally{state.busy=false;}
  }
  function parseClock(value){var p=String(value||"").split(":").map(Number);return p.length===2?p[0]*60+p[1]:null;}
  async function action(name, dateValue){
    try{
      if(name==="reload"){loadPlans();return;}
      if(name==="undo"){await undo();return;}
      if(name==="redo"){await redo();return;}
      if(name==="retry-sync"||name==="sync-now"){
        if(state.syncBusy)return;
        state.syncBusy=true;render();
        try{
          var syncResult=await api("/api/endurance/plans/"+state.plan.id+"/sync/retry",{method:"POST",body:"{}"});
          var result=syncResult.result||{};
          if(result.errors)toast("Sync ausgeführt: "+(result.synced||0)+" übertragen, "+result.errors+" benötigen eine Korrektur",true);
          else if(result.synced||result.deleted)toast("Intervals aktualisiert: "+(result.synced||0)+" übertragen"+(result.deleted?", "+result.deleted+" entfernt":""));
          else toast("Intervals geprüft – keine offenen Änderungen");
          await loadPlans(state.plan.id);
        }finally{state.syncBusy=false;render();}
        return;
      }
      if(name==="close-inspector"){state.inspectorOpen=false;render();return;}
      if(name==="prev"){state.start=addDays(state.start,-28);render();return;}
      if(name==="next"){state.start=addDays(state.start,28);render();return;}
      if(name==="today"){state.start=monday(new Date());render();return;}
      if(name==="create-plan"||name==="edit-goal"){
        var current=state.plan&&state.plan.event||{};
        var m=modal(name==="create-plan"?"Cardio-Ziel setzen":"Ziel bearbeiten",field("distance_m","Distanz (m)","number",current.distance_m||5000,'min="1" required')+field("target_time","Zielzeit (mm:ss)","text",current.target_time_s?clock(current.target_time_s):"",'placeholder="21:00"')+field("event_date","Eventdatum","date",current.event_date||"","required")+field("priority","Priorität","text",current.priority||"A")+field("title","Titel","text",current.title||""),"Ziel speichern");
        m.querySelector("form").onsubmit=async function(ev){ev.preventDefault();var f=new FormData(ev.target),event={distance_m:Number(f.get("distance_m")),target_time_s:parseClock(f.get("target_time")),event_date:f.get("event_date"),priority:f.get("priority"),title:f.get("title")};if(name==="create-plan"){var data=await api("/api/endurance/plans",{method:"POST",body:JSON.stringify({title:event.title||"Cardio-Plan",event:event})});m.remove();loadPlans(data.plan.id);}else{await patch([{op:"set_goal_event",event:event}]);m.remove();}};return;
      }
      if(name==="add-phase"){
        var last=state.plan.phases[state.plan.phases.length-1],start=last?iso(addDays(new Date(last.end_date+"T12:00:00"),1)):iso(state.start),end=iso(addDays(new Date(start+"T12:00:00"),27));
        var pm=modal("Phase hinzufügen",field("name","Name","text","Base","required")+field("phase_type","Typ","text","base")+field("start","Start","date",start,"required")+field("end","Ende","date",end,"required"),"Hinzufügen");
        pm.querySelector("form").onsubmit=async function(ev){ev.preventDefault();var f=new FormData(ev.target);await patch([{op:"add_phase",phase:{name:f.get("name"),phase_type:f.get("phase_type"),start_date:f.get("start"),end_date:f.get("end")}}]);pm.remove();};return;
      }
      if(dateValue||name==="add-session"){
        var sm=modal("Einheit hinzufügen",field("title","Titel","text","Easy Run","required")+field("type","Typ","text","easy")+field("date","Datum","date",dateValue||iso(state.start),"required")+field("duration","Dauer (min)","number","45"),"Hinzufügen");
        sm.querySelector("form").onsubmit=async function(ev){ev.preventDefault();var f=new FormData(ev.target);await patch([{op:"add_session",session:{title:f.get("title"),session_type:f.get("type"),scheduled_date:f.get("date"),duration_s:Number(f.get("duration"))*60,steps:[]}}]);sm.remove();};return;
      }
      var s=state.plan.sessions.find(function(x){return x.id===state.selectedId;});if(!s)return;
      if(name==="move-selected"){var mm=modal("Einheit verschieben",field("date","Neues Datum","date",s.scheduled_date,"required"),"Verschieben");mm.querySelector("form").onsubmit=async function(ev){ev.preventDefault();await patch([{op:"move_session",session_id:s.id,date:new FormData(ev.target).get("date")}]);mm.remove();};return;}
      if(name==="duplicate"){await patch([{op:"duplicate_session",session_id:s.id,date:iso(addDays(new Date(s.scheduled_date+"T12:00:00"),7))}]);toast("Einheit dupliziert");return;}
      if(name==="delete-session"){if(window.confirm("Einheit wirklich löschen?")){await patch([{op:"delete_session",session_id:s.id}],{confirm:true});state.selectedId=null;toast("Einheit gelöscht");}return;}
      if(name==="edit-steps"){
        var lines=flatSteps(s.steps||[]).map(function(x){return "  ".repeat(x.depth)+x.kind+"|"+(x.reps?x.reps+"x":x.duration_s?Math.round(x.duration_s/60)+"min":x.distance_m+"m")+"|"+(x.target&&x.target.zone||"open");}).join("\n");
        var em=modal("Workout-Schritte",'<label style="grid-column:1/-1;font-size:11px;color:#9aa7b3">Schritte – eine Zeile je Schritt<textarea name="steps" rows="9" style="display:block;width:100%;margin-top:5px;background:#0d141a;color:white;border:1px solid #34414d;border-radius:5px;padding:9px;font-size:14px">'+esc(lines)+'</textarea><small>Format: kind | 15min oder 800m | Zielzone</small></label>',"Schritte speichern");
        em.querySelector("form").onsubmit=async function(ev){ev.preventDefault();var roots=[],stack=[];String(new FormData(ev.target).get("steps")).split("\n").filter(Boolean).forEach(function(line){var spaces=(line.match(/^\s*/)||[""])[0].length,depth=Math.floor(spaces/2),x=line.trim().split("|").map(function(v){return v.trim();}),n=parseInt(x[1],10)||0,isMinutes=x[1]&&x[1].endsWith("min"),isRepeat=x[1]&&x[1].endsWith("x"),node={kind:x[0],reps:isRepeat?n:null,duration_s:isMinutes?n*60:null,distance_m:(!isMinutes&&!isRepeat)?n:null,target:{metric:"pace",basis:x[2]==="open"?"open":"fitness_anchor",zone:x[2]||"open"},steps:[]};if(depth===0)roots.push(node);else{var parent=stack[depth-1];if(parent)parent.steps.push(node);else roots.push(node);}stack[depth]=node;stack.length=depth+1;});await patch([{op:"replace_steps",session_id:s.id,steps:roots}]);em.remove();};return;
      }
      if(name==="set-anchor"){
        var a=state.plan.fitness_anchor||{},am=modal("Fitness Anchor",field("date","Datum","date",a.anchor_date||iso(new Date()),"required")+field("source","Quelle","text",a.source||"manual")+field("fivek","5K-Zeit (mm:ss)","text",a.five_k_time_s?clock(a.five_k_time_s):"")+field("threshold","Threshold Pace (mm:ss/km)","text",a.threshold_pace_s_per_km?clock(a.threshold_pace_s_per_km):""),"Anchor speichern");
        if(state.plan.fitness_anchor){var deleteAnchor=document.createElement("button");deleteAnchor.type="button";deleteAnchor.className="ep-btn danger";deleteAnchor.textContent="Anchor löschen";deleteAnchor.style.marginRight="auto";am.querySelector(".ep-actions").prepend(deleteAnchor);deleteAnchor.onclick=async function(){if(!window.confirm("Fitness Anchor wirklich löschen?"))return;try{await patch([{op:"clear_fitness_anchor"}],{confirm:true});am.remove();toast("Anchor gelöscht");}catch(e){toast(e.message,true);}};}
        am.querySelector("form").onsubmit=async function(ev){ev.preventDefault();var f=new FormData(ev.target);await patch([{op:"set_fitness_anchor",anchor:{date:f.get("date"),source:f.get("source"),five_k_time_s:parseClock(f.get("fivek")),threshold_pace_s_per_km:parseClock(f.get("threshold")),zones:{}}}]);am.remove();toast("Anchor gespeichert – zukünftige Ziele bleiben unverändert");};return;
      }
      if(name==="rebase"){
        var preview=(await api("/api/endurance/plans/"+state.plan.id+"/rebase-preview",{method:"POST",body:JSON.stringify({from_date:iso(new Date())})})).preview;
        var rows=preview.changes.slice(0,20).map(function(c){return "<p><small>"+c.date+"</small> "+(c.before&&c.before.pace_s_per_km?pace(c.before.pace_s_per_km):"offen")+" → "+(c.after&&c.after.pace_s_per_km?pace(c.after.pace_s_per_km):"offen")+"</p>";}).join("");
        var rm=modal("Künftige Ziele aktualisieren",'<div style="grid-column:1/-1;color:#cbd5de;font-size:13px"><strong>'+preview.changed+' Zielbereiche ändern sich</strong><p>'+preview.unchanged+' bleiben unverändert'+(preview.past_changed?' · '+preview.past_changed+' vergangene Einheiten betroffen':'')+'</p><div style="max-height:220px;overflow:auto">'+rows+"</div></div>","Übernehmen");
        rm.querySelector("form").onsubmit=async function(ev){ev.preventDefault();await patch([{op:"rebase_future_targets",from_date:iso(new Date())}]);rm.remove();toast("Zukünftige Targets aktualisiert");};return;
      }
    }catch(e){toast(e.message,true);}
  }
  function bindPhaseInteraction(el){
    var index=Number(el.dataset.index),phases=state.plan.phases.slice().sort(function(a,b){return (a.sort_order-b.sort_order)||a.start_date.localeCompare(b.start_date);}),ph=phases[index],track=el.closest(".ep-timeline"),total=Number(track.dataset.totalDays)||1;
    function daysFor(dx){return Math.round(dx/Math.max(1,track.clientWidth)*total);}
    function run(operation){patch([operation]).catch(function(e){toast(e.message,true);render();});}
    el.onpointerdown=function(e){
      if(e.button!==0||e.target.closest("[data-resize]")||index===0||index===phases.length-1)return;
      e.preventDefault();var startX=e.clientX,delta=0,previous=phases[index-1],next=phases[index+1],previousEl=track.querySelector('[data-phase="'+previous.id+'"]'),nextEl=track.querySelector('[data-phase="'+next.id+'"]'),previousDays=dayCount(previous.start_date,previous.end_date),nextDays=dayCount(next.start_date,next.end_date),currentDays=dayCount(ph.start_date,ph.end_date);
      var minDelta=-(previousDays-1),maxDelta=nextDays-1;
      el.setPointerCapture(e.pointerId);el.classList.add("is-dragging");
      el.onpointermove=function(move){delta=clamp(daysFor(move.clientX-startX),minDelta,maxDelta);var newStart=iso(addDays(asDate(ph.start_date),delta)),newEnd=iso(addDays(asDate(ph.end_date),delta));previousEl.style.flexBasis=((previousDays+delta)/total*100)+"%";el.style.flexBasis=(currentDays/total*100)+"%";nextEl.style.flexBasis=((nextDays-delta)/total*100)+"%";previousEl.querySelector("[data-phase-duration]").textContent=(Math.round((previousDays+delta)/7*10)/10).toLocaleString("de-DE")+" Wochen";previousEl.querySelector("[data-phase-dates]").textContent=shortDate(previous.start_date)+" – "+shortDate(iso(addDays(asDate(newStart),-1)));el.querySelector("[data-phase-dates]").textContent=shortDate(newStart)+" – "+shortDate(newEnd);nextEl.querySelector("[data-phase-duration]").textContent=(Math.round((nextDays-delta)/7*10)/10).toLocaleString("de-DE")+" Wochen";nextEl.querySelector("[data-phase-dates]").textContent=shortDate(iso(addDays(asDate(newEnd),1)))+" – "+shortDate(next.end_date);el.dataset.preview=(delta>0?"+":"")+delta+" Tage";};
      el.onpointerup=function(){el.onpointermove=null;el.onpointerup=null;el.classList.remove("is-dragging");delete el.dataset.preview;if(!delta){render();return;}run({op:"move_phase",phase_id:ph.id,start_date:iso(addDays(asDate(ph.start_date),delta)),end_date:iso(addDays(asDate(ph.end_date),delta))});};
      el.onpointercancel=function(){render();};
    };
    el.querySelectorAll("[data-resize]").forEach(function(handle){handle.onpointerdown=function(e){
      if(e.button!==0)return;e.preventDefault();e.stopPropagation();var startX=e.clientX,delta=0,side=handle.dataset.resize,neighbor=side==="start"?phases[index-1]:phases[index+1],neighborEl=track.querySelector('[data-phase="'+neighbor.id+'"]'),originalDays=dayCount(ph.start_date,ph.end_date),neighborDays=dayCount(neighbor.start_date,neighbor.end_date);
      var minDelta=side==="start"?-(neighborDays-1):-(originalDays-1),maxDelta=side==="start"?originalDays-1:neighborDays-1;
      handle.setPointerCapture(e.pointerId);el.classList.add("is-resizing");neighborEl.classList.add("is-resizing-neighbor");
      handle.onpointermove=function(move){delta=clamp(daysFor(move.clientX-startX),minDelta,maxDelta);var current=side==="start"?originalDays-delta:originalDays+delta,adjacent=side==="start"?neighborDays+delta:neighborDays-delta;el.style.flexBasis=(current/total*100)+"%";neighborEl.style.flexBasis=(adjacent/total*100)+"%";var start=side==="start"?iso(addDays(asDate(ph.start_date),delta)):ph.start_date,end=side==="end"?iso(addDays(asDate(ph.end_date),delta)):ph.end_date,neighborStart=side==="end"?iso(addDays(asDate(end),1)):neighbor.start_date,neighborEnd=side==="start"?iso(addDays(asDate(start),-1)):neighbor.end_date;el.querySelector("[data-phase-duration]").textContent=(Math.round(current/7*10)/10).toLocaleString("de-DE")+" Wochen";el.querySelector("[data-phase-dates]").textContent=shortDate(start)+" – "+shortDate(end);neighborEl.querySelector("[data-phase-duration]").textContent=(Math.round(adjacent/7*10)/10).toLocaleString("de-DE")+" Wochen";neighborEl.querySelector("[data-phase-dates]").textContent=shortDate(neighborStart)+" – "+shortDate(neighborEnd);};
      handle.onpointerup=function(){handle.onpointermove=null;handle.onpointerup=null;el.classList.remove("is-resizing");neighborEl.classList.remove("is-resizing-neighbor");if(!delta){render();return;}run({op:"resize_phase",phase_id:ph.id,start_date:side==="start"?iso(addDays(asDate(ph.start_date),delta)):ph.start_date,end_date:side==="end"?iso(addDays(asDate(ph.end_date),delta)):ph.end_date});};
      handle.onpointercancel=function(){render();};
    };});
  }
  function bind(){
    var select=root.querySelector(".ep-plan-select");if(select)select.onchange=function(e){state.selectedId=null;loadPlans(e.target.value);};
    root.querySelectorAll("[data-action]").forEach(function(el){el.onclick=function(){action(el.dataset.action);};});
    root.querySelectorAll("[data-add-date]").forEach(function(el){el.onclick=function(){action("add-session",el.dataset.addDate);};});
    root.querySelectorAll("[data-metric]").forEach(function(el){el.onclick=function(){state.metric=el.dataset.metric;render();};});
    root.querySelectorAll("[data-load]").forEach(function(el){el.onclick=function(){state.loadMetric=el.dataset.load;render();};});
    root.querySelectorAll("[data-calendar-view]").forEach(function(el){el.onclick=function(){state.calendarView=el.dataset.calendarView;render();};});
    root.querySelectorAll("[data-inspector-tab]").forEach(function(el){el.onclick=function(){state.inspectorTab=el.dataset.inspectorTab;render();};});
    root.querySelectorAll("[data-session]").forEach(function(el){el.onclick=function(){state.selectedId=el.dataset.session;var session=state.plan.sessions.find(function(item){return item.id===el.dataset.session;});state.metric=preferredMetric(session);state.inspectorOpen=true;render();};el.ondragstart=function(e){e.dataTransfer.setData("text/session",el.dataset.session);};});
    root.querySelectorAll(".ep-day").forEach(function(el){el.ondragover=function(e){if(Array.from(e.dataTransfer.types).includes("text/session")){e.preventDefault();el.classList.add("is-drop");}};el.ondragleave=function(){el.classList.remove("is-drop");};el.ondrop=async function(e){e.preventDefault();el.classList.remove("is-drop");var id=e.dataTransfer.getData("text/session");if(id)await patch([{op:"move_session",session_id:id,date:el.dataset.date}]);};});
    root.querySelectorAll("[data-phase]").forEach(bindPhaseInteraction);
  }
  window.addEventListener("planning:tab",function(e){if(e.detail&&e.detail.tab==="cardio"&&!state.plan&&!state.plans.length)loadPlans();});
  document.addEventListener("keydown",function(e){
    var key=String(e.key).toLowerCase();
    if(!(e.ctrlKey||e.metaKey)||e.altKey||e.shiftKey||(key!=="z"&&key!=="y")||root.offsetParent===null)return;
    var target=e.target;if(target&&(target.matches("input,textarea,[contenteditable=true]")||target.closest(".ep-modal")))return;
    e.preventDefault();action(key==="z"?"undo":"redo");
  });
  loadPlans();
})();
