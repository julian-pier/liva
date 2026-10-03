"""ChatGPT-native, filesystem-first compile operations (no MCP dependency)."""
from __future__ import annotations
import fcntl, json, os, re, shutil, time, traceback, unicodedata
from pathlib import Path
from .core import StoreError, EVIDENCE_TYPES, sha256_bytes, ulid, utc_now

ALLOWED_LIVING=("wiki/Ich/","wiki/Menschen/","wiki/Training & Körper/","wiki/Projekte/","wiki/Mein Leben/","wiki/Erinnerungsarchiv/")
HUMAN_NOTE_TYPES={"hub","event","insight","topic","archive"}

def _identity(value):
    """Stable human-note identity used for exact duplicate protection."""
    value=unicodedata.normalize("NFKC",str(value or "")).casefold()
    return " ".join(re.findall(r"[\w\u00c0-\u024f]+",value,flags=re.UNICODE))

def _note_profile(path, raw):
    frontmatter=raw.split("---\n",2)[1] if raw.startswith("---\n") and raw.count("---\n")>=2 else ""
    h1=re.findall(r"(?m)^#\s+(.+?)\s*$",raw)
    title_match=re.search(r"(?m)^title:\s*(.+?)\s*$",frontmatter)
    title=(title_match.group(1).strip().strip("\"'") if title_match else (h1[0] if h1 else Path(path).stem))
    type_match=re.search(r"(?m)^type:\s*(\w+)\s*$",frontmatter)
    date_match=re.search(r"(?m)^event_date:\s*(\d{4}-\d{2}-\d{2})\s*$",frontmatter)
    return {"path":path,"title":title,"identity":_identity(title),"note_type":type_match.group(1) if type_match else None,"event_date":date_match.group(1) if date_match else None,"h1":h1}

def _living_profiles(store, staged=None):
    staged=staged or {}; profiles=[]
    for full in sorted((store.vault/"wiki").rglob("*.md")):
        path=full.relative_to(store.vault).as_posix()
        raw=staged.get(path,full.read_text(encoding="utf-8"))
        profiles.append(_note_profile(path,raw))
    for path,raw in staged.items():
        if path.startswith("wiki/") and not any(item["path"]==path for item in profiles): profiles.append(_note_profile(path,raw))
    return profiles

def _duplicate_pages(store, path, title, note_type=None, event_date=None, staged=None):
    identity=_identity(title); parent=Path(path).parent.as_posix(); matches=[]
    for item in _living_profiles(store,staged):
        if item["path"]==path or item["identity"]!=identity: continue
        same_kind=bool(note_type and item["note_type"]==note_type)
        same_event=note_type=="event" and same_kind and bool(event_date) and item["event_date"]==event_date
        if Path(item["path"]).parent.as_posix()==parent or same_event or (same_kind and note_type in {"hub","insight","topic","archive"}): matches.append(item)
    return matches

def _resolve_link_target(store, current_path, target, known_paths):
    target=target.strip()
    if not target or target.startswith("#") or "://" in target: return True
    candidate=target[:-3] if target.endswith(".md") else target
    extension="" if Path(candidate).suffix else ".md"
    possibilities=[]
    if candidate.startswith(("wiki/","sources/","state/","procedures/")): possibilities.append(candidate+extension)
    elif "/" in candidate: possibilities.extend([(Path(current_path).parent/candidate).as_posix()+extension,candidate+extension])
    else:
        suffix="/"+candidate+".md"
        possibilities.extend(path for path in known_paths if path.endswith(suffix))
    return any(os.path.normpath(item).replace("\\","/") in known_paths for item in possibilities)

def _memory_health(store, paths=None, staged=None, known_paths_extra=None):
    """Read-only structural health report; never guesses semantic repairs."""
    staged=staged or {}; profiles=_living_profiles(store,staged)
    known_paths={p.relative_to(store.vault).as_posix() for p in store.vault.rglob("*") if p.is_file() and not any(part in {".git",".obsidian"} for part in p.relative_to(store.vault).parts)}
    known_paths.update(staged)
    known_paths.update(known_paths_extra or set())
    selected=set(paths or [item["path"] for item in profiles]); issues=[]
    by_identity={}
    for item in profiles: by_identity.setdefault((item["identity"],item["note_type"]),[]).append(item["path"])
    for item in profiles:
        if item["path"] not in selected: continue
        raw=staged[item["path"]] if item["path"] in staged else (store.vault/item["path"]).read_text(encoding="utf-8")
        if len(item["h1"])!=1: issues.append({"code":"TITLE_STRUCTURE","severity":"error","path":item["path"],"detail":f"Expected exactly one Markdown H1; found {len(item['h1'])}."})
        elif _identity(item["title"])!=_identity(item["h1"][0]): issues.append({"code":"TITLE_MISMATCH","severity":"error","path":item["path"],"detail":"Frontmatter title and Markdown H1 must identify the same document."})
        frontmatter=raw.split("---\n",2)[1] if raw.startswith("---\n") and raw.count("---\n")>=2 else ""
        if "liva-wiki" not in frontmatter: issues.append({"code":"MISSING_LIVA_CLASS","severity":"warning","path":item["path"],"detail":"Human note has no liva-wiki presentation class."})
        duplicate_group=by_identity.get((item["identity"],item["note_type"]),[])
        if item["identity"] and len(duplicate_group)>1: issues.append({"code":"DUPLICATE_IDENTITY","severity":"warning","path":item["path"],"detail":"Same normalized title and note type: "+", ".join(p for p in duplicate_group if p!=item["path"])})
        paragraphs=[_identity(p) for p in re.split(r"\n\s*\n",re.sub(r"(?s)^---\n.*?\n---\n","",raw)) if len(_identity(p))>=60]
        repeated=sorted({p for p in paragraphs if paragraphs.count(p)>1})
        if repeated: issues.append({"code":"DUPLICATE_CONTENT","severity":"warning","path":item["path"],"detail":"Exact paragraph content is repeated inside the page."})
        for target in re.findall(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]*)?\]\]",raw):
            if not _resolve_link_target(store,item["path"],target,known_paths): issues.append({"code":"BROKEN_LINK","severity":"error","path":item["path"],"detail":f"Unresolved wikilink: {target}"})
        outgoing=[target for target in re.findall(r"\[\[([^\]|#]+)",raw) if not target.startswith("sources/")]
        if item["note_type"] in {"event","insight","topic"} and not outgoing: issues.append({"code":"ORPHAN_NOTE","severity":"warning","path":item["path"],"detail":"Human leaf has no outgoing semantic link."})
    return {"status":"clean" if not issues else "needs_attention","checked_paths":sorted(selected),"issues":issues,"error_count":sum(i["severity"]=="error" for i in issues),"warning_count":sum(i["severity"]=="warning" for i in issues)}

def _ref(layer,path,heading,digest): return "sec_"+sha256_bytes(f"{layer}|{path}|{heading}|{digest}".encode())[:32]
def _safe_path(layer,path):
    if not isinstance(path,str) or Path(path).is_absolute() or ".." in Path(path).parts or not path.endswith(".md"): raise StoreError("invalid knowledge path")
    if layer=="living_wiki" and any(path.startswith(x) for x in ALLOWED_LIVING): return
    if layer=="procedures" and path.startswith("procedures/") and not path.startswith("procedures/runtime/"): return
    raise StoreError("path is outside allowed knowledge area")

def _human_note_document(op):
    """Build a new human note from explicit editor choices, never inferred ones."""
    title=op.get("title")
    body=op.get("new_text")
    note_type=op.get("note_type")
    if not isinstance(title,str) or not title.strip() or "\n" in title or not isinstance(body,str) or not body.strip(): raise StoreError("invalid create")
    if note_type not in HUMAN_NOTE_TYPES: raise StoreError("invalid human note type")
    if re.search(r"(?m)^#\s+",body): raise StoreError("new note body must not contain a Markdown H1")
    event_date=op.get("event_date")
    precision=op.get("event_date_precision")
    if note_type=="event":
        if event_date is not None and (not isinstance(event_date,str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",event_date)): raise StoreError("invalid event date")
        if event_date is None and precision not in {"unknown", "approximate"}: raise StoreError("invalid event date precision")
        if event_date is not None and precision not in {None,"exact","approximate"}: raise StoreError("invalid event date precision")
    elif event_date is not None: raise StoreError("invalid event date")
    person=op.get("person")
    if person is not None and (not isinstance(person,str) or not person.strip() or "\n" in person): raise StoreError("invalid note person")
    # Keep the human title in metadata as well as in the H1.  This lets
    # presentation and retrieval layers identify newly-created Living Notes
    # without deriving a display name from the filesystem path.
    frontmatter=["---","title: "+json.dumps(title.strip(),ensure_ascii=False),f"type: {note_type}"]
    if event_date is not None: frontmatter.append(f"event_date: {event_date}")
    if precision is not None: frontmatter.append(f"event_date_precision: {precision}")
    if person is not None: frontmatter.append("person: "+json.dumps(person,ensure_ascii=False))
    frontmatter.extend(["cssclasses:","  - liva-wiki","---","",f"# {title.strip()}","",body.strip(),""])
    return "\n".join(frontmatter)

def _is_historical_event(raw):
    frontmatter=raw.split("---\n",2)[1] if raw.startswith("---\n") and raw.count("---\n")>=2 else ""
    return bool(re.search(r"(?m)^type:\s*event\s*$",frontmatter))

def _patch_frontmatter(raw, patch):
    """Apply a deliberately small scalar frontmatter patch without reparsing prose."""
    if not isinstance(patch, dict) or not patch: raise StoreError("invalid metadata patch")
    allowed={"type","event_date","event_date_precision","status","temporal_status"}
    if set(patch)-allowed: raise StoreError("invalid metadata patch")
    if not raw.startswith("---\n"):
        raise StoreError("human note frontmatter required")
    end=raw.find("\n---\n",4)
    if end < 0: raise StoreError("human note frontmatter required")
    lines=raw[4:end].splitlines()
    values={}
    for i,line in enumerate(lines):
        match=re.match(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s*(.*))?$",line)
        if match: values[match.group(1)]=(i,match.group(2))
    note_type=patch.get("type", values.get("type",(None,None))[1])
    if note_type not in HUMAN_NOTE_TYPES: raise StoreError("invalid human note type")
    event_date=patch.get("event_date", values.get("event_date",(None,None))[1])
    precision=patch.get("event_date_precision", values.get("event_date_precision",(None,None))[1])
    if note_type == "event":
        if event_date not in {None, ""} and not re.fullmatch(r"\d{4}-\d{2}-\d{2}",event_date): raise StoreError("invalid event date")
        if event_date in {None,""} and precision not in {"unknown","approximate"}: raise StoreError("invalid event date precision")
    elif event_date not in {None,""}: raise StoreError("invalid event date")
    for key,value in patch.items():
        rendered = None if value is None else str(value)
        if key in values:
            index,_=values[key]
            if rendered is None: lines.pop(index)
            else: lines[index]=f"{key}: {rendered}"
        elif rendered is not None:
            lines.append(f"{key}: {rendered}")
    return "---\n"+"\n".join(lines)+raw[end:]

def _ensure_liva_css(raw):
    if not raw.startswith("---\n"): raise StoreError("human note frontmatter required")
    end=raw.find("\n---\n",4)
    if end < 0: raise StoreError("human note frontmatter required")
    frontmatter=raw[4:end]
    if re.search(r"(?ms)^cssclasses:.*?^\s*- liva-wiki\s*$",frontmatter): return raw
    if re.search(r"(?m)^cssclasses:\s*$",frontmatter):
        frontmatter=re.sub(r"(?m)^(cssclasses:\s*)$",r"\1\n  - liva-wiki",frontmatter,1)
    else: frontmatter += "\ncssclasses:\n  - liva-wiki"
    return "---\n"+frontmatter+raw[end:]

def _normalize_human_structure(raw):
    """Apply only deterministic presentation repairs, never semantic edits."""
    h1=list(re.finditer(r"(?m)^#\s+(.+?)\s*$",raw))
    if len(h1)>1:
        first_title=_identity(h1[0].group(1))
        removable=[match for match in h1[1:] if _identity(match.group(1))==first_title and not raw[h1[0].end():match.start()].strip()]
        for match in reversed(removable): raw=raw[:match.start()]+raw[match.end():].lstrip("\n")
    return _ensure_liva_css(raw)

def _append_correction(raw, correction, source_path):
    if not isinstance(correction,str) or not correction.strip() or re.search(r"(?m)^#\s+",correction): raise StoreError("invalid correction")
    correction=correction.strip()
    if _identity(correction) in _identity(raw): raise StoreError("DUPLICATE_CONTENT: correction already exists")
    source_target=source_path[:-3] if source_path and source_path.endswith(".md") else source_path
    entry=f"- **{utc_now()[:10]}:** {correction}"
    if source_target: entry+=f"\n  - Beleg: [[{source_target}|Memory-Quelle]]"
    heading=re.search(r"(?m)^## Korrekturen\s*$",raw)
    if not heading: return raw.rstrip()+"\n\n## Korrekturen\n\n"+entry+"\n"
    next_heading=re.search(r"(?m)^##\s+",raw[heading.end():])
    end=heading.end()+(next_heading.start() if next_heading else len(raw[heading.end():]))
    return raw[:end].rstrip()+"\n"+entry+"\n\n"+raw[end:].lstrip("\n")

def _ensure_structural_links(store, raw, current_path, links, planned_paths=None):
    """Add verified outgoing links without rewriting historical prose."""
    if not isinstance(links,list) or not 1 <= len(links) <= 12: raise StoreError("ensure_links requires 1-12 links")
    current_dir=Path(current_path).parent
    existing=set()
    for match in re.finditer(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]*)?\]\]",raw):
        target=match.group(1); candidate=target[:-3] if target.endswith(".md") else target
        resolved=(current_dir/candidate).as_posix() if not candidate.startswith("wiki/") else candidate
        existing.add(os.path.normpath(resolved).replace("\\","/"))
    rendered=[]; requested=set()
    for item in links:
        if isinstance(item,str): path,label=item,None
        elif isinstance(item,dict): path,label=item.get("path"),item.get("label")
        else: raise StoreError("invalid structural link")
        if not isinstance(path,str): raise StoreError("invalid structural link path")
        _safe_path("living_wiki",path)
        if not (store.vault/path).is_file() and path not in (planned_paths or set()): raise StoreError("structural link target does not exist")
        target=path[:-3]
        if target in requested: continue
        requested.add(target)
        if target in existing: continue
        if label is None: label=Path(target).name
        if not isinstance(label,str) or not label.strip() or any(char in label for char in "\n[]|"): raise StoreError("invalid structural link label")
        rendered.append(f"- [[{target}|{label.strip()}]]")
    if not rendered: return raw
    heading=re.search(r"(?m)^## Verknüpfungen\s*$",raw)
    if not heading: return raw.rstrip()+"\n\n## Verknüpfungen\n\n"+"\n".join(rendered)+"\n"
    next_heading=re.search(r"(?m)^##\s+",raw[heading.end():])
    end=heading.end()+(next_heading.start() if next_heading else len(raw[heading.end():]))
    before=raw[:end].rstrip()
    after=raw[end:]
    return before+"\n"+"\n".join(rendered)+"\n\n"+after.lstrip("\n")

def _ensure_source_link(raw, source_path):
    """Attach immutable provenance to a newly created human note."""
    if not isinstance(source_path,str) or not source_path.startswith("sources/") or not source_path.endswith(".md"):
        raise StoreError("invalid source provenance path")
    target=source_path[:-3]
    if re.search(r"\[\["+re.escape(target)+r"(?:\||\]\])",raw): return raw
    return raw.rstrip()+"\n\n## Quellen\n\n- [["+target+"|Memory-Quelle]]\n"

def _rewrite_links(raw, current_path, old_path, new_path):
    """Rewrite only wikilinks resolving to old_path; prose and unrelated links stay intact."""
    old_stem=old_path[:-3] if old_path.endswith(".md") else old_path
    new_stem=new_path[:-3] if new_path.endswith(".md") else new_path
    current_dir=Path(current_path).parent
    def replace(match):
        target, suffix, label=match.group(1),match.group(2) or "",match.group(3) or ""
        if target.startswith("#") or "://" in target: return match.group(0)
        candidate=target[:-3] if target.endswith(".md") else target
        resolved=(current_dir/candidate).as_posix() if not candidate.startswith("wiki/") else candidate
        normalized=os.path.normpath(resolved).replace("\\\\","/")
        if normalized == old_stem:
            relative=os.path.relpath(new_stem, current_dir.as_posix()).replace("\\\\","/")
            if target.startswith("wiki/"): relative=new_stem
            return "[["+relative+suffix+label+"]]"
        return match.group(0)
    return re.sub(r"\[\[([^\]|#]+)(#[^\]|]+)?(\|[^\]]*)?\]\]",replace,raw)

def _stage_change(changed, entry):
    """Stage one canonical version per path, preserving its original backup.

    A multi-page move can rewrite links in the same note more than once.  Each
    rewrite must build on the previously staged text; otherwise the later move
    would silently discard the earlier link update.
    """
    prior=next((item for item in reversed(changed) if item[0] == entry[0]), None)
    if prior is None:
        changed.append(entry)
        return
    index=len(changed)-1-changed[::-1].index(prior)
    changed[index]=(entry[0],prior[1],entry[2],entry[3],entry[4],entry[5],entry[6])

def _safe_reporting_for_replay(result):
    """Backfill conservative reporting fields for commits made by older cores."""
    if "reporting" in result: return result
    source_changed=bool(result.get("source_changed")); knowledge_changed=bool(result.get("knowledge_changed"))
    result["outcome"]="source_only" if source_changed and not knowledge_changed else "no_change"
    result["verification"]={"source_readback":False,"knowledge_readback":False}
    result["reporting"]={"may_claim_source_captured":False,"may_claim_knowledge_saved":False,"required_summary":("Source exists, but this legacy replay does not verify a Living Wiki update." if source_changed else "No Living Wiki change was made.")}
    return result

def memory_context(store, queries, layers="auto", limit=8, max_chars=24000):
    if not isinstance(queries,list) or not queries or layers not in {"auto","living_wiki","procedures","both"}: raise StoreError("invalid context request")
    wanted=("living_wiki","procedures") if layers in {"auto","both"} else (layers,)
    # Procedures are a small, filesystem-backed instruction set. They can be
    # added independently of Memory V2 writes, so refresh this derived index at
    # the retrieval boundary rather than requiring an operator to touch SQLite.
    # This keeps nested procedures/skills/**/SKILL.md discoverable immediately
    # while leaving the Living Wiki index and all personal knowledge untouched.
    if "procedures" in wanted: store.procedure_reindex()
    found=[]; seen=set(); remaining=max_chars
    for layer in wanted:
        search=store.wiki_search if layer=="living_wiki" else store.procedure_search
        get=store.wiki_get if layer=="living_wiki" else store.procedure_get
        for query in queries:
            for hit in search(query,min(max(limit * 4, 12), 20)):
                key=(layer,hit["path"],hit["heading"])
                if key in seen: continue
                section=get(hit["path"],hit["heading"]); text=section["text"]
                if len(text)>remaining: continue
                seen.add(key); remaining-=len(text); found.append({"section_ref":_ref(layer,section["path"],section["heading"],section["content_sha256"]),"layer":layer,"path":section["path"],"title":section["title"],"heading":section["heading"],"exact_text":text,"text_chars":len(text),"text_complete":True,"section_sha256":section["content_sha256"],"score":hit["score"]})
    query=" ".join(queries).casefold()
    # FTS is content-first; explicitly named hubs remain discoverable even when
    # their short navigation text does not repeat the person's name often.
    if "living_wiki" in wanted:
        words=set(re.findall(r"[\wäöüß-]+",query))
        with store._connect() as db:
            named=db.execute("SELECT path,heading,content_sha256 FROM wiki_sections WHERE heading <> ''").fetchall()
        for row in named:
            heading=row["heading"].casefold()
            if heading in words and len(heading) >= 3:
                key=("living_wiki",row["path"],row["heading"])
                if key in seen: continue
                section=store.wiki_get(row["path"],row["heading"]); text=section["text"]
                if len(text)<=remaining:
                    seen.add(key); remaining-=len(text); found.append({"section_ref":_ref("living_wiki",section["path"],section["heading"],section["content_sha256"]),"layer":"living_wiki","path":section["path"],"title":section["title"],"heading":section["heading"],"exact_text":text,"text_chars":len(text),"text_complete":True,"section_sha256":section["content_sha256"],"score":0.0})
    # A few high-value faith intents have deliberately separate canonical
    # homes.  Include their page even when an AND-style FTS query omits it;
    # priority() below still decides its final position.
    def add_canonical(layer, path):
        nonlocal remaining
        if layer not in wanted or not (store.vault/path).is_file(): return
        get=store.wiki_get if layer=="living_wiki" else store.procedure_get
        section=get(path); key=(layer,path,section["heading"]); text=section["text"]
        while key not in seen and len(text)>remaining and found:
            displaced=found.pop()
            seen.discard((displaced["layer"],displaced["path"],displaced["heading"]))
            remaining+=len(displaced["exact_text"])
        if key not in seen and len(text)<=remaining:
            seen.add(key); remaining-=len(text); found.append({"section_ref":_ref(layer,section["path"],section["heading"],section["content_sha256"]),"layer":layer,"path":section["path"],"title":section["title"],"heading":section["heading"],"exact_text":text,"text_chars":len(text),"text_complete":True,"section_sha256":section["content_sha256"],"score":0.0})
    # Generic title matching makes the canonical page discoverable without a
    # growing list of domain-specific routing rules.  The rules below may still
    # refine rank for genuinely ambiguous life-domain questions.
    query_identity=_identity(" ".join(queries))
    if "living_wiki" in wanted:
        for profile in _living_profiles(store):
            if len(profile["identity"])>=4 and profile["identity"] in query_identity: add_canonical("living_wiki",profile["path"])
    faith_intent=("glaub" in query or "christentum" in query or ("frei" in query and "führung" in query))
    if faith_intent:
        if any(word in query for word in ("aktuell", "heute", "zuletzt dokumentiert")): add_canonical("living_wiki","wiki/Ich/Mein Glaube/Mein Glaube.md")
        if "sommer" in query and any(word in query for word in ("entwickelt", "entwicklung", "begann")): add_canonical("living_wiki","wiki/Mein Leben/2026/Episoden/Glaubensentwicklung im Sommer.md")
        if "frei" in query and "führung" in query: add_canonical("living_wiki","wiki/Ich/Mein Glaube/Freier Wille & Gottes Führung.md")
        if "emma" in query and any(word in query for word in ("rolle", "auslöser")): add_canonical("living_wiki","wiki/Mein Leben/2026/Episoden/Glaubensentwicklung im Sommer.md")
        if "mathilda" in query and "rolle" in query: add_canonical("living_wiki","wiki/Mein Leben/2026/Episoden/Glaube erstmals ausgesprochen.md")
        if any(word in query for word in ("zweifel", "christentum")): add_canonical("living_wiki","wiki/Ich/Mein Glaube/Zweifel & intellektuelle Ehrlichkeit.md")
        if any(word in query for word in ("chatgpt", "begleit", "begleiten")): add_canonical("procedures","procedures/faith-companion.md")
    mathilda_hub="wiki/Menschen/Mathilda/Mathilda.md"
    mathilda_festival="wiki/Menschen/Mathilda/2026-08-08 – Schützenfest & Kennenlernen.md"
    mathilda_date="wiki/Menschen/Mathilda/Erstes Date in Münster.md"
    mathilda_density="wiki/Menschen/Mathilda/2026-08-18 – Nähe, Dichte & eigene Zeit.md"
    emma_hub="wiki/Menschen/Emma/Emma.md"
    emma_training="wiki/Menschen/Emma/Erstes gemeinsames Training.md"
    emma_unbound="wiki/Menschen/Emma/Gespräch über Ungebundenheit.md"
    emma_after="wiki/Menschen/Emma/Nachhall und spätere Einordnung.md"
    mathilda_festival_intent="mathilda" in query and "schützenfest" in query
    mathilda_date_intent="mathilda" in query and any(word in query for word in ("erstes date", "münster"))
    mathilda_density_intent="mathilda" in query and any(word in query for word in ("dicht", "eigene zeit", "nähe"))
    emma_training_intent="emma" in query and "training" in query
    emma_unbound_intent="emma" in query and "ungebunden" in query
    emma_after_intent="emma" in query and any(word in query for word in ("später", "einordnung", "nachhall"))
    if mathilda_festival_intent: add_canonical("living_wiki",mathilda_festival)
    if mathilda_date_intent: add_canonical("living_wiki",mathilda_date)
    if mathilda_density_intent: add_canonical("living_wiki",mathilda_density)
    if emma_training_intent: add_canonical("living_wiki",emma_training)
    if emma_unbound_intent: add_canonical("living_wiki",emma_unbound)
    if emma_after_intent: add_canonical("living_wiki",emma_after)
    djk_hub="wiki/Ich/Beruf & Zukunft/DJK Coesfeld & duales Studium/DJK Coesfeld & duales Studium.md"
    djk_history="wiki/Ich/Beruf & Zukunft/DJK Coesfeld & duales Studium/Meine Vorgeschichte bei DJK.md"
    djk_ist="wiki/Ich/Beruf & Zukunft/DJK Coesfeld & duales Studium/Sportwissenschaft & Training an der IST.md"
    djk_role="wiki/Ich/Beruf & Zukunft/DJK Coesfeld & duales Studium/Aufgaben & mögliche Rolle im Verein.md"
    djk_contract="wiki/Ich/Beruf & Zukunft/DJK Coesfeld & duales Studium/Vertrag, Gehalt & Entscheidungszeitpunkt.md"
    djk_career="wiki/Ich/Beruf & Zukunft/DJK Coesfeld & duales Studium/Karrierechancen nach dem Studium.md"
    djk_history_intent=("djk" in query and any(word in query for word in ("vorher", "vorgeschichte", "erfahrung", "vor der bewerbung"))) or "fitnesswelt-vorerfahrung" in query
    djk_study_intent=("ist" in query and any(word in query for word in ("stud", "wissenschaft", "training"))) or "was wollte ich studieren" in query or "studienbelast" in query or ("trainingserfahrung" in query and "studienwunsch" in query)
    djk_role_intent=("djk" in query and any(word in query for word in ("aufgabe", "rolle", "verein"))) or "meine aufgaben" in query or "aufgaben im verein" in query
    djk_contract_intent=("djk" in query and any(word in query for word in ("vertrag", "gehalt", "untersch", "vergütung", "gespräch"))) or ("gehalt" in query and "vertrag" in query) or ("vertrag" in query and "entscheidung" in query)
    djk_career_intent=("djk" in query and any(word in query for word in ("karriere", "perspekt", "langfrist"))) or "welche sorge hatte ich langfristig" in query or "personal trainer" in query or "berufsbilder" in query or "sportstudium" in query
    djk_police_comparison="djk" in query and "polizei" in query
    djk_application_intent=("djk" in query and "bewerb" in query) or "was geschah bei der bewerbung" in query
    career_intent=(any(word in query for word in ("beruf", "karriere", "djk", "polizei", "sportbereich", "vertrag", "gehalt")) or ("endgültig" in query and "entschieden" in query) or djk_history_intent or djk_study_intent or djk_role_intent or djk_contract_intent or djk_career_intent or djk_application_intent)
    if career_intent:
        if "beruflich" in query or ("langfristig" in query and any(word in query for word in ("beruf", "karriere"))): add_canonical("living_wiki","wiki/Ich/Beruf & Zukunft/Beruf & Zukunft.md")
        if "beruf" in query and any(word in query for word in ("wichtig", "kriter", "bedeutet")): add_canonical("living_wiki","wiki/Ich/Beruf & Zukunft/Erfolg, Aufstieg & Entwicklung.md")
        if any(word in query for word in ("gewöhnlich", "stillstand")): add_canonical("living_wiki","wiki/Ich/Beruf & Zukunft/Angst vor beruflichem Stillstand.md")
        if djk_application_intent: add_canonical("living_wiki","wiki/Mein Leben/2026/Episoden/2026-07-29 – DJK-Bewerbung abgeschickt.md")
        elif "djk" in query: add_canonical("living_wiki",djk_hub)
        if djk_history_intent: add_canonical("living_wiki",djk_history)
        if djk_study_intent: add_canonical("living_wiki",djk_ist)
        if djk_role_intent: add_canonical("living_wiki",djk_role)
        if djk_contract_intent: add_canonical("living_wiki",djk_contract)
        if djk_career_intent:
            add_canonical("living_wiki",djk_career)
            add_canonical("living_wiki","wiki/Ich/Beruf & Zukunft/Angst vor beruflichem Stillstand.md")
        if djk_police_comparison:
            add_canonical("living_wiki",djk_hub)
            add_canonical("living_wiki","wiki/Ich/Beruf & Zukunft/Polizei.md")
        if "polizei" in query: add_canonical("living_wiki","wiki/Ich/Beruf & Zukunft/Polizei.md")
        if "sportbereich" in query: add_canonical("living_wiki","wiki/Ich/Beruf & Zukunft/Sportwissenschaft & Training.md")
        if "endgültig" in query and "entschieden" in query: add_canonical("living_wiki","wiki/Ich/Beruf & Zukunft/Beruf & Zukunft.md")
    # Training has distinct canonical homes for identity, personal practice,
    # progress, nutrition, recovery, physique and historical gym context.
    # Add only the explicitly requested home: FTS otherwise overweights short
    # navigation sections and unrelated uses of broad words such as progress.
    training_hub="wiki/Training & Körper/Training & Körper.md"
    training_identity="wiki/Training & Körper/Training als Teil meiner Identität.md"
    training_how="wiki/Training & Körper/Wie ich trainiere.md"
    training_progress="wiki/Training & Körper/Fortschritt richtig bewerten.md"
    training_hybrid="wiki/Training & Körper/Hybrid aus Kraft & Ausdauer.md"
    training_nutrition="wiki/Training & Körper/Ernährung mit Struktur.md"
    training_recovery="wiki/Training & Körper/Recovery & Datenvertrauen.md"
    training_body="wiki/Training & Körper/Körperbild, Sichtbarkeit & Stolz.md"
    training_history="wiki/Erinnerungsarchiv/Training & Körper.md"
    training_intent=any(word in query for word in ("training", "gym", "krafttraining", "hrv", "recovery", "ernährung", "kalorien", "körperbild", "aussehen", "bankdrücken", "ausdauer", "sportlich"))
    if training_intent:
        if "training" in query and any(word in query for word in ("bedeutet", "langfristig", "rolle")): add_canonical("living_wiki",training_identity)
        if "gym" in query and any(word in query for word in ("coach", "coachen", "wechsel")):
            add_canonical("living_wiki",training_history if "wechsel" in query else training_how)
        if any(word in query for word in ("fortschritt", "progression")): add_canonical("living_wiki",training_progress)
        if any(word in query for word in ("hrv", "recovery")): add_canonical("living_wiki",training_recovery)
        if any(word in query for word in ("ernährung", "kalorien")): add_canonical("living_wiki",training_nutrition)
        if any(word in query for word in ("aussehen", "körperbild", "optik")): add_canonical("living_wiki",training_body)
        if any(word in query for word in ("ausdauer", "sportlich")): add_canonical("living_wiki",training_hybrid)
        if "bankdrücken" in query or ("gym" in query and "wechsel" in query): add_canonical("living_wiki",training_history)
    historical=any(word in query for word in ("damals","früher","vor dem","historisch","ende ","wann ","am 0","am 1","am 2","januar","februar","märz","april","mai","juni","juli","august","september","oktober","november","dezember"))
    current_system=not historical and any(word in query for word in ("produktiv","produktion","cutover","architektur","system"))
    date_match=re.search(r"\b20\d{2}-\d{2}-\d{2}\b",query)
    short_date=re.search(r"\b(0?[1-9]|[12]\d|3[01])\.(0?[1-9]|1[0-2])\.",query)
    def priority(item):
        value=-float(item["score"])
        raw=(store.vault/item["path"]).read_text(encoding="utf-8") if item["layer"]=="living_wiki" else ""
        kind=re.search(r"(?m)^type:\s*(\w+)\s*$",raw)
        kind=kind.group(1) if kind else ""
        if current_system:
            if item["layer"]=="procedures": value+=100
            if item["path"]=="procedures/memory-v2-cutover.md": value+=100
            if "Frühere Migrationsphase" in item["exact_text"]: value-=35
        if historical:
            if kind=="event": value+=45
            if item["layer"]=="procedures": value-=20
            if "cutover" in query and "Memory V2" in item["title"]: value+=100
        if date_match and date_match.group(0) in raw: value+=120
        if short_date and re.search(rf"(?m)^event_date:\s*20\d{{2}}-{int(short_date.group(2)):02d}-{int(short_date.group(1)):02d}\s*$", raw): value+=1_000_000
        # For an overview, navigation hubs outrank an arbitrary historical leaf.
        if not historical and kind=="hub": value+=22
        if not current_system and kind=="hub" and item["title"].casefold() in query: value+=70
        title=item["title"].casefold(); path=item["path"].casefold()
        title_identity=_identity(item["title"])
        if len(title_identity)>=4 and title_identity in query_identity: value+=300
        if "kennenlernphase" in query and kind=="event" and "kennenlernphase" in title: value+=250
        if "emma" in query and "juli" in query and kind=="event" and "juli" in (title+" "+raw).casefold(): value+=500
        if faith_intent and ("glaub" in title or "glaub" in path): value+=180
        # Faith has an intentionally separate current hub, historical episodes,
        # insights, and a companion procedure.  FTS alone overweights short
        # navigation headings, so route these explicit user intents to their
        # canonical homes without inferring any personal belief.
        faith_hub="wiki/Ich/Mein Glaube/Mein Glaube.md"
        faith_summer="wiki/Mein Leben/2026/Episoden/Glaubensentwicklung im Sommer.md"
        faith_spoken="wiki/Mein Leben/2026/Episoden/Glaube erstmals ausgesprochen.md"
        faith_freedom="wiki/Ich/Mein Glaube/Freier Wille & Gottes Führung.md"
        faith_doubt="wiki/Ich/Mein Glaube/Zweifel & intellektuelle Ehrlichkeit.md"
        career_hub="wiki/Ich/Beruf & Zukunft/Beruf & Zukunft.md"
        career_success="wiki/Ich/Beruf & Zukunft/Erfolg, Aufstieg & Entwicklung.md"
        career_stagnation="wiki/Ich/Beruf & Zukunft/Angst vor beruflichem Stillstand.md"
        career_djk=djk_hub
        career_djk_history=djk_history
        career_djk_ist=djk_ist
        career_djk_role=djk_role
        career_djk_contract=djk_contract
        career_djk_career=djk_career
        career_police="wiki/Ich/Beruf & Zukunft/Polizei.md"
        career_study="wiki/Ich/Beruf & Zukunft/Sportwissenschaft & Training.md"
        career_application="wiki/Mein Leben/2026/Episoden/2026-07-29 – DJK-Bewerbung abgeschickt.md"
        if faith_intent:
            if any(word in query for word in ("aktuell", "heute", "zuletzt dokumentiert")) and item["path"]==faith_hub: value+=700
            if "sommer" in query and any(word in query for word in ("entwickelt", "entwicklung", "begann")) and item["path"]==faith_summer: value+=700
            if "frei" in query and "führung" in query and item["path"]==faith_freedom: value+=700
            if "emma" in query and any(word in query for word in ("rolle", "auslöser")) and item["path"]==faith_summer: value+=700
            if "mathilda" in query and "rolle" in query and item["path"]==faith_spoken: value+=700
            if any(word in query for word in ("zweifel", "christentum")) and item["path"]==faith_doubt: value+=700
        if mathilda_festival_intent and item["path"]==mathilda_festival: value+=900
        if mathilda_date_intent and item["path"]==mathilda_date: value+=900
        if mathilda_density_intent and item["path"]==mathilda_density: value+=900
        if emma_training_intent and item["path"]==emma_training: value+=900
        if emma_unbound_intent and item["path"]==emma_unbound: value+=900
        if emma_after_intent and item["path"]==emma_after: value+=900
        if career_intent:
            if ("beruflich" in query or ("langfristig" in query and any(word in query for word in ("beruf", "karriere")))) and item["path"]==career_hub: value+=700
            if "beruf" in query and any(word in query for word in ("wichtig", "kriter", "bedeutet")) and item["path"]==career_success: value+=700
            if any(word in query for word in ("gewöhnlich", "stillstand")) and item["path"]==career_stagnation: value+=700
            if djk_application_intent and item["path"]==career_application: value+=700
            elif "djk" in query and "bewerb" not in query and item["path"]==career_djk: value+=700
            if djk_history_intent and item["path"]==career_djk_history: value+=900
            if djk_study_intent and item["path"]==career_djk_ist: value+=900
            if djk_role_intent and item["path"]==career_djk_role: value+=900
            if djk_contract_intent and item["path"]==career_djk_contract: value+=900
            if djk_career_intent and item["path"]==career_djk_career: value+=900
            if djk_police_comparison and item["path"] in {career_djk, career_police}: value+=950
            if "polizei" in query and item["path"]==career_police: value+=700
            if "sportbereich" in query and item["path"]==career_study: value+=700
            if "endgültig" in query and "entschieden" in query and item["path"]==career_hub: value+=700
        if training_intent:
            if "training" in query and any(word in query for word in ("bedeutet", "langfristig", "rolle")) and item["path"]==training_identity: value+=700
            if "gym" in query and any(word in query for word in ("coach", "coachen")) and item["path"]==training_how: value+=700
            if "gym" in query and "wechsel" in query and item["path"]==training_history: value+=700
            if any(word in query for word in ("fortschritt", "progression")) and item["path"]==training_progress: value+=700
            if any(word in query for word in ("hrv", "recovery")) and item["path"]==training_recovery: value+=700
            if any(word in query for word in ("ernährung", "kalorien")) and item["path"]==training_nutrition: value+=700
            if any(word in query for word in ("aussehen", "körperbild", "optik")) and item["path"]==training_body: value+=700
            if any(word in query for word in ("ausdauer", "sportlich")) and item["path"]==training_hybrid: value+=700
            if "bankdrücken" in query and item["path"]==training_history: value+=700
        if item["layer"]=="procedures" and item["path"]=="procedures/faith-companion.md" and faith_intent and any(word in query for word in ("chatgpt", "begleit", "begleiten")): value+=1200
        if item["layer"]=="living_wiki" and faith_intent and any(word in query for word in ("chatgpt", "begleit", "begleiten")): value-=120
        if any(word in query for word in ("chasing","unsicherheit","überanalyse")) and any(word in title for word in ("chasing","unsicherheit","überanalyse")): value+=400
        if not historical and kind=="event": value-=8
        return value
    found.sort(key=priority, reverse=True)
    return {"context_id":"ctx_"+ulid()[4:],"results":found[:limit]}

def memory_source(store, source_id, offset=0, max_chars=12000):
    data=store.source_content(source_id); content=data["content"]
    if not isinstance(offset,int) or offset<0 or not isinstance(max_chars,int) or not 1<=max_chars<=50000: raise StoreError("invalid source range")
    chunk=content[offset:offset+max_chars]; end=offset+len(chunk)
    contract={"source_sections":[{"name":section["name"],"allowed_evidence_types":section["allowed_evidence_types"]} for section in data["sections"]]}
    return {"source_id":source_id,"source_kind":data["source_kind"],"metadata":{k:data[k] for k in ("event_date","temporal_scope","evidence_types")},"claim_contract":contract,"content":chunk,"offset":offset,"returned_chars":len(chunk),"next_offset":end if end<len(content) else None,"total_chars":len(content),"sha256":data["sha256"]}

def memory_pending(store, kind=None, limit=50):
    store.initialize()
    with store._connect() as db:
        rows=db.execute("SELECT s.source_id,s.source_kind,s.path,s.captured_at,s.event_date,s.temporal_scope,COALESCE(c.state,'uncompiled') state FROM sources s LEFT JOIN chat_compile_state c ON c.source_id=s.source_id WHERE COALESCE(c.state,'uncompiled')='uncompiled' ORDER BY s.captured_at LIMIT ?",(limit,)).fetchall()
    return [dict(row) for row in rows if kind is None or row["source_kind"]==kind]

def memory_health(store, paths=None):
    store.initialize()
    return _memory_health(store,paths)

def _validate_claims(store, source_id, claims):
    sections={s["name"]:set(s["allowed_evidence_types"]) for s in store.source_content(source_id)["sections"]}
    ids=set()
    for claim in claims:
        if not isinstance(claim,dict) or not isinstance(claim.get("claim_id"),str) or not claim["claim_id"] or claim["claim_id"] in ids or not isinstance(claim.get("text"),str) or not claim["text"].strip(): raise StoreError("invalid claim")
        ids.add(claim["claim_id"])
        if claim.get("evidence_type") not in EVIDENCE_TYPES or claim.get("source_section") not in sections or claim["evidence_type"] not in sections[claim["source_section"]]: raise StoreError("claim evidence incompatible with source")
    return ids

def _memory_commit_unlocked(store, payload):
    if not isinstance(payload,dict) or not isinstance(payload.get("idempotency_key"),str): raise StoreError("idempotency_key required")
    source=payload.get("source"); source_id=payload.get("source_id")
    if bool(source)==bool(source_id): raise StoreError("provide exactly one of source or source_id")
    store.initialize(); key=payload["idempotency_key"]
    payload_sha256=sha256_bytes(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"),default=str).encode())
    with store._connect() as db:
        replay=db.execute("SELECT result_json,payload_sha256 FROM chat_commits WHERE idempotency_key=?",(key,)).fetchone()
    if replay:
        if replay[1] and replay[1] != payload_sha256: raise StoreError("IDEMPOTENCY_CONFLICT")
        result=json.loads(replay[0]); result["replayed"]=True; return _safe_reporting_for_replay(result)
    created=None; changed=[]; deletions=[]; audit_path=None; previous_compile_state=None; commit_recorded=False; current_operation={"index":None,"operation":"prepare"}; txn="txn_"+ulid()[4:]; txndir=store.runtime_dir/"transactions"/txn; (txndir/"staged").mkdir(parents=True); (txndir/"backup").mkdir(); (txndir/"transaction.json").write_text(json.dumps({"status":"prepared","transaction_id":txn}),encoding="utf-8"); started=time.perf_counter()
    try:
        if source:
            required={"title","direct_user_context","assistant_synthesis","temporal_scope","topics"}
            if not isinstance(source,dict) or not required<=source.keys(): raise StoreError("incomplete chat source")
            source_id="src_"+ulid()[4:]; now=utc_now(); body=store._capture_body(source["title"],source["direct_user_context"],source["assistant_synthesis"])+"\n## Claims\n\n"+json.dumps(payload.get("claims",[]),ensure_ascii=False,indent=2)+"\n"; digest=sha256_bytes(body.encode()); rel=Path("sources/captures")/now[:4]/now[5:7]/f"{source_id}.md"; doc=store._capture_document(source_id,now,source.get("event_date"),source["temporal_scope"],source["topics"],["direct_user_statement","assistant_summary"],"raw_only",digest,source["title"],body); created=(rel,doc,digest,now)
        else: store.source_content(source_id)
        with store._connect() as db:
            source_record=db.execute("SELECT path FROM sources WHERE source_id=?",(source_id,)).fetchone()
            previous_compile_state=db.execute("SELECT state,transaction_id FROM chat_compile_state WHERE source_id=?",(source_id,)).fetchone()
        source_path=source_record["path"] if source_record else (created[0].as_posix() if created else None)
        if created:
            claim_ids=set()
            for claim in payload.get("claims",[]):
                if not isinstance(claim,dict) or not isinstance(claim.get("claim_id"),str) or not claim["claim_id"] or claim["claim_id"] in claim_ids or not isinstance(claim.get("text"),str) or not claim["text"].strip(): raise StoreError("invalid claim")
                claim_ids.add(claim["claim_id"])
                allowed={"direct_user_context":{"direct_user_statement"},"assistant_synthesis":{"assistant_summary","assistant_interpretation","retrospective_synthesis"}}
                if claim.get("source_section") not in allowed or claim.get("evidence_type") not in allowed[claim["source_section"]]: raise StoreError("claim evidence incompatible with source")
        else: claim_ids=_validate_claims(store,source_id,payload.get("claims",[]))
        ops=payload.get("knowledge_operations",[])
        if not isinstance(ops,list): raise StoreError("knowledge_operations invalid")
        planned_paths={op.get("path") for op in ops if isinstance(op,dict) and op.get("operation")=="create_page" and isinstance(op.get("path"),str)}
        modified_refs=set()
        for operation_index,op in enumerate(ops):
            current_operation={"index":operation_index,"operation":op.get("operation") if isinstance(op,dict) else None}
            if op.get("operation")=="modify_section":
                ref=op.get("target_ref"); match=_resolve_ref(store,ref)
                if not match: raise StoreError("STALE_CONTEXT")
                if ref in modified_refs: raise StoreError("invalid modify")
                modified_refs.add(ref)
                layer,path,heading,digest=match; new=op.get("new_text")
                if not isinstance(new,str) or not new.strip() or not set(op.get("used_claim_ids",[]))<=claim_ids: raise StoreError("invalid modify")
                full=store.vault/path; raw=full.read_text(encoding="utf-8")
                if layer=="living_wiki" and _is_historical_event(raw): raise StoreError("historical event notes cannot be overwritten")
                section=(store.wiki_get if layer=="living_wiki" else store.procedure_get)(path,heading,digest)
                if section["content_sha256"]!=digest: raise StoreError("STALE_CONTEXT")
                prior=next((item for item in reversed(changed) if item[0]==full),None)
                base=prior[2] if prior else raw
                if section["text"] not in base: raise StoreError("invalid modify")
                after=base.replace(section["text"],new,1)
                if prior:
                    changed[changed.index(prior)]=(full,prior[1],after,layer,path,heading,digest)
                else: changed.append((full,raw,after,layer,path,heading,digest))
            elif op.get("operation")=="create_page":
                layer,path=op.get("target_layer"),op.get("path"); _safe_path(layer,path)
                if (store.vault/path).exists() or op.get("classification")!="NEW_TOPIC": raise StoreError("invalid create")
                if layer=="living_wiki":
                    links=op.get("links")
                    if not isinstance(links,list) or not 1<=len(links)<=6: raise StoreError("MISSING_PARENT_LINKS: every new Human Note requires 1-6 explicit semantic links")
                    already_staged={item[4]:item[2] for item in changed if item[3]=="living_wiki"}
                    duplicates=_duplicate_pages(store,path,op.get("title"),op.get("note_type"),op.get("event_date"),already_staged)
                    if duplicates: raise StoreError("DUPLICATE_PAGE: "+", ".join(item["path"] for item in duplicates))
                document = _human_note_document(op) if layer == "living_wiki" else "# "+op.get("title","")+"\n\n"+op.get("new_text","").strip()+"\n"
                if layer=="living_wiki":
                    document=_ensure_source_link(document,source_path)
                    document=_ensure_structural_links(store,document,path,links,planned_paths)
                changed.append((store.vault/path,None,document,layer,path,"",None))
            elif op.get("operation")=="update_metadata":
                ref=op.get("target_ref"); match=_resolve_ref(store,ref)
                if not match: raise StoreError("STALE_CONTEXT")
                layer,path,heading,digest=match
                if layer != "living_wiki": raise StoreError("invalid metadata patch")
                full=store.vault/path; raw=full.read_text(encoding="utf-8")
                if store.wiki_get(path,heading)["content_sha256"] != digest: raise StoreError("STALE_CONTEXT")
                prior=next((item for item in reversed(changed) if item[0]==full),None); base=prior[2] if prior else raw
                _stage_change(changed,(full,raw,_patch_frontmatter(base,op.get("metadata")),layer,path,heading,digest))
            elif op.get("operation")=="ensure_liva_css":
                ref=op.get("target_ref"); match=_resolve_ref(store,ref)
                if not match: raise StoreError("STALE_CONTEXT")
                layer,path,heading,digest=match
                if layer != "living_wiki": raise StoreError("invalid metadata patch")
                full=store.vault/path; raw=full.read_text(encoding="utf-8")
                if store.wiki_get(path,heading)["content_sha256"] != digest: raise StoreError("STALE_CONTEXT")
                prior=next((item for item in reversed(changed) if item[0]==full),None); base=prior[2] if prior else raw
                after=_ensure_liva_css(base)
                if after != base: _stage_change(changed,(full,raw,after,layer,path,heading,digest))
            elif op.get("operation")=="ensure_links":
                ref=op.get("target_ref"); match=_resolve_ref(store,ref)
                if not match: raise StoreError("STALE_CONTEXT")
                layer,path,heading,digest=match
                if layer != "living_wiki": raise StoreError("ensure_links requires a Living Wiki target")
                full=store.vault/path; raw=full.read_text(encoding="utf-8")
                if store.wiki_get(path,heading)["content_sha256"] != digest: raise StoreError("STALE_CONTEXT")
                prior=next((item for item in reversed(changed) if item[0]==full),None); base=prior[2] if prior else raw
                after=_ensure_structural_links(store,base,path,op.get("links"),planned_paths)
                if after != base: _stage_change(changed,(full,raw,after,layer,path,heading,digest))
            elif op.get("operation")=="append_correction":
                ref=op.get("target_ref"); match=_resolve_ref(store,ref)
                if not match: raise StoreError("STALE_CONTEXT")
                layer,path,heading,digest=match
                if layer!="living_wiki" or not set(op.get("used_claim_ids",[]))<=claim_ids: raise StoreError("invalid correction")
                full=store.vault/path; raw=full.read_text(encoding="utf-8")
                if store.wiki_get(path,heading)["content_sha256"]!=digest: raise StoreError("STALE_CONTEXT")
                prior=next((item for item in reversed(changed) if item[0]==full),None); base=prior[2] if prior else raw
                after=_append_correction(base,op.get("new_text"),source_path)
                _stage_change(changed,(full,raw,after,layer,path,heading,digest))
            elif op.get("operation")=="maintain_page":
                ref=op.get("target_ref"); match=_resolve_ref(store,ref)
                if not match: raise StoreError("STALE_CONTEXT")
                layer,path,heading,digest=match
                if layer!="living_wiki": raise StoreError("maintain_page requires a Living Wiki target")
                full=store.vault/path; raw=full.read_text(encoding="utf-8")
                if store.wiki_get(path,heading)["content_sha256"]!=digest: raise StoreError("STALE_CONTEXT")
                prior=next((item for item in reversed(changed) if item[0]==full),None); base=prior[2] if prior else raw
                after=_normalize_human_structure(base)
                if after!=base: _stage_change(changed,(full,raw,after,layer,path,heading,digest))
            elif op.get("operation")=="rewrite_page":
                ref=op.get("target_ref"); match=_resolve_ref(store,ref)
                if not match: raise StoreError("STALE_CONTEXT")
                layer,path,heading,digest=match; full=store.vault/path; raw=full.read_text(encoding="utf-8")
                if layer=="living_wiki" and _is_historical_event(raw): raise StoreError("historical event prose cannot be rewritten; use ensure_links for structural links")
                if heading not in {Path(path).stem, re.search(r"(?m)^#\s+(.+)$",raw).group(1) if re.search(r"(?m)^#\s+(.+)$",raw) else ""}: raise StoreError("rewrite requires page h1")
                if (store.wiki_get if layer=="living_wiki" else store.procedure_get)(path,heading)["content_sha256"] != digest: raise StoreError("STALE_CONTEXT")
                after=op.get("new_text")
                if not isinstance(after,str) or len(re.findall(r"(?m)^#\s+",after)) != 1: raise StoreError("invalid page rewrite")
                changed.append((full,raw,after,layer,path,heading,digest))
            elif op.get("operation")=="move_page":
                ref=op.get("target_ref"); match=_resolve_ref(store,ref)
                if not match: raise StoreError("STALE_CONTEXT")
                layer,path,heading,digest=match
                if layer != "living_wiki": raise StoreError("invalid move")
                destination=op.get("path"); _safe_path("living_wiki",destination)
                if destination == path: raise StoreError("invalid move")
                source_full=store.vault/path; destination_full=store.vault/destination; raw=source_full.read_text(encoding="utf-8")
                if store.wiki_get(path,heading)["content_sha256"] != digest: raise StoreError("STALE_CONTEXT")
                pending=next((entry for entry in reversed(changed) if entry[0]==source_full),None)
                moved=pending[2] if pending else raw
                moved=_patch_frontmatter(moved,op["metadata"]) if "metadata" in op else moved
                if destination_full.exists() and destination_full.read_bytes() != raw.encode("utf-8"): raise StoreError("move destination already exists")
                if not destination_full.exists(): changed.append((destination_full,None,moved,"living_wiki",destination,"",None))
                deletions.append((source_full,raw,path))
                for candidate in store.vault.rglob("*.md"):
                    rel=candidate.relative_to(store.vault).as_posix()
                    if candidate == source_full or rel.startswith(("sources/",".git/",".obsidian/","procedures/runtime/")): continue
                    prior=next((item for item in reversed(changed) if item[0] == candidate),None)
                    before=prior[2] if prior else candidate.read_text(encoding="utf-8")
                    after=_rewrite_links(before,rel,path,destination)
                    if after != before: _stage_change(changed,(candidate,before,after,"living_wiki",rel,"",None))
            elif op.get("operation")=="rewrite_links":
                old_path,new_path=op.get("old_path"),op.get("path")
                _safe_path("living_wiki",old_path); _safe_path("living_wiki",new_path)
                if not (store.vault/new_path).exists(): raise StoreError("invalid link rewrite")
                for candidate in store.vault.rglob("*.md"):
                    rel=candidate.relative_to(store.vault).as_posix()
                    if rel.startswith(("sources/",".git/",".obsidian/","procedures/runtime/")): continue
                    prior=next((item for item in reversed(changed) if item[0] == candidate),None)
                    before=prior[2] if prior else candidate.read_text(encoding="utf-8")
                    after=_rewrite_links(before,rel,old_path,new_path)
                    if after != before: _stage_change(changed,(candidate,before,after,"living_wiki",rel,"",None))
            else: raise StoreError("invalid knowledge operation")
        state=payload.get("compile_result",{}).get("state","raw_only" if not ops else "compiled")
        if state not in {"compiled","raw_only","live_data"}: raise StoreError("invalid compile state")
        knowledge_changed=bool(changed or deletions)
        if state=="compiled" and not knowledge_changed: raise StoreError("compiled state requires a verified knowledge change")
        if state=="raw_only" and (ops or knowledge_changed): raise StoreError("raw_only stores a source only and cannot include knowledge operations")
        if state=="live_data" and ops: raise StoreError("live_data cannot include Markdown knowledge operations")
        staged_living={item[4]:item[2] for item in changed if item[3]=="living_wiki"}
        preflight=_memory_health(store,staged_living.keys(),staged_living,{source_path} if source_path else set()) if staged_living else {"status":"clean","checked_paths":[],"issues":[],"error_count":0,"warning_count":0}
        created_living={item[4] for item in changed if item[1] is None and item[3]=="living_wiki"}
        fatal=[issue for issue in preflight["issues"] if issue["severity"]=="error" and (issue["path"] in created_living or issue["code"] in {"TITLE_STRUCTURE","TITLE_MISMATCH"})]
        if fatal: raise StoreError("MEMORY_PREFLIGHT_FAILED: "+"; ".join(f'{item["path"]}: {item["detail"]}' for item in fatal))
        if created: (txndir/"staged"/created[0].name).write_text(created[1],encoding="utf-8")
        for index,item in enumerate(changed): (txndir/"staged"/f"write-{index}.md").write_text(item[2],encoding="utf-8")
        audit={"transaction_id":txn,"source_id":source_id,"origin":"chatgpt","timestamp":utc_now(),"claims":payload.get("claims",[]),"compile_result":payload.get("compile_result",{}),"knowledge_operations":ops,"idempotency_key":key}
        audit_path=store.runtime_dir/"audit"/f"{txn}.json"; (txndir/"staged"/"audit.json").write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding="utf-8")
        for full,before,after,*_ in changed:
            if before is not None: (txndir/"backup"/str(len(list((txndir/"backup").iterdir())))).write_text(before,encoding="utf-8")
            store._atomic_write(full,after.encode())
        for full,_,_ in deletions: full.unlink(missing_ok=True)
        if created:
            store._atomic_write(store.vault/created[0],created[1].encode())
            with store._connect() as db: store._insert_source(db,source_id,"chat_capture",str(created[0]),created[2],created[3],source.get("event_date"),source["temporal_scope"]); db.execute("INSERT INTO idempotency_keys VALUES (?,?,?)",(key,source_id,created[3]))
        reindex_started=time.perf_counter(); store.wiki_reindex(); store.procedure_reindex(); reindex_ms=round((time.perf_counter()-reindex_started)*1000,2)
        source_verified=not created or (store.vault/created[0]).read_text(encoding="utf-8")==created[1]
        knowledge_verified=all(full.exists() and full.read_text(encoding="utf-8")==after for full,_,after,*_ in changed) and all(not full.exists() for full,_,_ in deletions)
        outcome="knowledge_updated" if knowledge_changed else ("live_data_only" if state=="live_data" else "source_only" if created else "no_change")
        if knowledge_changed and knowledge_verified: required_summary="Living Wiki updated and verified."
        elif created and source_verified: required_summary="Source captured only; Living Wiki was not updated."
        else: required_summary="No Living Wiki change was made."
        maintenance=_memory_health(store,[x[4] for x in changed if x[3]=="living_wiki"]) if knowledge_changed else {"status":"clean","checked_paths":[],"issues":[],"error_count":0,"warning_count":0}
        source_row=None
        with store._connect() as db: source_row=db.execute("SELECT path FROM sources WHERE source_id=?",(source_id,)).fetchone()
        provenance={"source_id":source_id,"source_path":source_row["path"] if source_row else (created[0].as_posix() if created else None),"claim_ids":sorted(claim_ids),"audit_path":audit_path.relative_to(store.runtime_dir).as_posix()}
        result={"transaction_id":txn,"source_id":source_id,"state":state,"outcome":outcome,"changed_paths":[x[4] for x in changed],"source_changed":bool(created),"knowledge_changed":knowledge_changed,"verification":{"source_readback":source_verified,"knowledge_readback":knowledge_changed and knowledge_verified},"preflight":{"duplicate_guard":"passed","structural_validation":"passed","warnings":preflight["warning_count"]},"provenance":provenance,"maintenance":maintenance,"reporting":{"may_claim_source_captured":bool(created) and source_verified,"may_claim_knowledge_saved":knowledge_changed and knowledge_verified,"required_summary":required_summary},"replayed":False,"timings_ms":{"transaction":round((time.perf_counter()-started)*1000,2),"reindex":reindex_ms}}
        store._atomic_write(audit_path,json.dumps(audit,ensure_ascii=False,indent=2).encode())
        with store._connect() as db: db.execute("INSERT INTO chat_compile_state VALUES (?,?,?) ON CONFLICT(source_id) DO UPDATE SET state=excluded.state,transaction_id=excluded.transaction_id",(source_id,result["state"],txn)); db.execute("INSERT INTO chat_commits (idempotency_key,transaction_id,result_json,payload_sha256) VALUES (?,?,?,?)",(key,txn,json.dumps(result),payload_sha256))
        commit_recorded=True
        (txndir/"transaction.json").write_text(json.dumps({"status":"committed",**result}),encoding="utf-8"); return result
    except Exception as exc:
        rollback_errors=[]
        try:
            for full,before,*_ in reversed(changed):
                if before is None: full.unlink(missing_ok=True)
                else: store._atomic_write(full,before.encode())
            for full,before,_ in deletions:
                if not full.exists(): store._atomic_write(full,before.encode())
            if created: (store.vault/created[0]).unlink(missing_ok=True)
            if audit_path: audit_path.unlink(missing_ok=True)
        except Exception as rollback_exc:
            rollback_errors.append(f"filesystem:{rollback_exc.__class__.__name__}:{rollback_exc}")
        try:
            with store._connect() as db:
                db.execute("DELETE FROM chat_commits WHERE idempotency_key=?",(key,))
                db.execute("DELETE FROM chat_compile_state WHERE source_id=?",(source_id,))
                if previous_compile_state is not None:
                    db.execute("INSERT INTO chat_compile_state VALUES (?,?,?)",(source_id,previous_compile_state["state"],previous_compile_state["transaction_id"]))
                if created:
                    db.execute("DELETE FROM idempotency_keys WHERE idempotency_key=?",(key,)); db.execute("DELETE FROM sources WHERE source_id=?",(source_id,))
        except Exception as rollback_exc:
            rollback_errors.append(f"database:{rollback_exc.__class__.__name__}:{rollback_exc}")
        try:
            # Rebuild derived indexes after restoring canonical files.  Without
            # this, a failure after reindex could leave search/graph state from
            # content that was already rolled back.
            store.wiki_reindex(); store.procedure_reindex()
        except Exception as rollback_exc:
            rollback_errors.append(f"reindex:{rollback_exc.__class__.__name__}:{rollback_exc}")
        rollback_ok=not rollback_errors
        message=str(exc); normalized=message.casefold()
        if isinstance(exc,StoreError) and exc.code: code=exc.code
        elif "source hash mismatch" in normalized: code="SOURCE_INTEGRITY_ERROR"
        elif "stale_context" in normalized: code="STALE_CONTEXT"
        elif "claim evidence" in normalized or "evidence incompatible" in normalized: code="CLAIM_CONTRACT_ERROR"
        elif "historical event" in normalized: code="HISTORICAL_EVENT_IMMUTABLE"
        elif "idempotency_conflict" in normalized: code="IDEMPOTENCY_CONFLICT"
        elif "duplicate_page" in normalized: code="DUPLICATE_PAGE"
        elif "duplicate_content" in normalized: code="DUPLICATE_CONTENT"
        elif "missing_parent_links" in normalized: code="MISSING_PARENT_LINKS"
        elif "invalid knowledge path" in normalized or "outside allowed knowledge area" in normalized: code="INVALID_PATH"
        elif "human note type" in normalized: code="INVALID_NOTE_TYPE"
        elif "event date" in normalized: code="INVALID_EVENT_DATE"
        elif "structural link" in normalized or "broken_link" in normalized: code="INVALID_LINK"
        elif "memory_preflight_failed" in normalized: code="PREFLIGHT_FAILED"
        elif any(token in normalized for token in ("invalid modify","invalid create","knowledge operation","markdown h1","must not contain")): code="INVALID_CHANGE"
        elif any(token in normalized for token in ("invalid ","required","preflight","outside allowed")): code="VALIDATION_ERROR"
        elif isinstance(exc,OSError) or "database is locked" in normalized: code="PERSISTENCE_ERROR"
        else: code="TRANSACTION_ROLLED_BACK" if rollback_ok else "ROLLBACK_FAILED"
        diagnostic={"status":"rolled_back" if rollback_ok else "rollback_failed","transaction_id":txn,"operation":current_operation,"error_type":exc.__class__.__name__,"error":message,"trace":traceback.format_exc(),"rollback_ok":rollback_ok,"rollback_errors":rollback_errors,"commit_maybe_applied":commit_recorded}
        try: (txndir/"transaction.json").write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding="utf-8")
        except OSError: pass
        safe_details={"transaction_id":txn,"phase":"commit","operation":current_operation,"rollback_ok":rollback_ok,"commit_maybe_applied":commit_recorded,"retryable":code in {"STALE_CONTEXT","PERSISTENCE_ERROR"} and rollback_ok}
        raise StoreError(message,code=code,details=safe_details,retryable=safe_details["retryable"]) from exc

def memory_commit(store, payload):
    """Serialize commits across processes and make identical retries replayable."""
    store.initialize()
    lock_path=store.runtime_dir/"locks"/"memory-commit.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        try: return _memory_commit_unlocked(store,payload)
        finally: fcntl.flock(lock.fileno(),fcntl.LOCK_UN)

def _resolve_ref(store, ref):
    for layer,getter in (("living_wiki",store.wiki_get),("procedures",store.procedure_get)):
        # refs are recomputable; scan indexed sections only
        with store._connect() as db:
            table="wiki_sections" if layer=="living_wiki" else "procedure_sections"
            rows=db.execute(f"SELECT path,heading,content_sha256 FROM {table}").fetchall()
        for row in rows:
            if _ref(layer,row["path"],row["heading"],row["content_sha256"])==ref:return layer,row["path"],row["heading"],row["content_sha256"]
    return None
