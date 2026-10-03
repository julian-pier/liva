"""Canonical Memory V2 instruction delivered through every MCP transport."""

MEMORY_SKILL = """Canonical long-term memory is LIVA Memory V2 in the Vault, never legacy useMemos.

For every durable memory write, use this exact workflow:
1. Recall or call liva_memory_begin. begin captures only an immutable source and searches canonical Wiki/Procedure candidates; it is never proof that Living knowledge was saved.
2. Inspect candidates, workflow and maintenance_context. Read the relevant existing page before editing it. Existing knowledge is the default target; create_page is allowed only for a genuinely new semantic unit.
3. Classify the information: durable personal knowledge -> living_wiki; agent behavior -> procedures; historical evidence -> immutable source plus an optional event; transient current state -> current-state/live tools; operational measurements -> canonical live data.
4. Call liva_memory_finish once with source-grounded claims and the smallest coherent set of changes. Use replace_section to update Living knowledge, ensure_links for verified structural links, correct_page for a demonstrated factual correction that must preserve historical prose, and create_page only after candidate review shows no canonical equivalent. Every create_page must provide 1-6 explicit Living Wiki links with the real main hub/parent included. The backend derives the canonical folder and dated event filename from that parent. It rejects missing or ambiguous parents instead of creating an unconnected or guessed page.
5. Inspect preflight, provenance, verification, maintenance and reporting. Only reporting.may_claim_knowledge_saved=true permits saying that the Wiki was saved, repaired or read back. If it is false, explicitly say that the source alone was captured or that no Wiki change was made. If maintenance.status=needs_attention, name the remaining issue and do not call the result fully clean.

Memory Gardener is the canonical organizational policy: one meaningful human note per event, durable insight, focused topic or hub; never one file per sentence or chat message. Hubs navigate rather than duplicate their children. Link instead of repeating the same fact. The backend rejects exact page-identity duplicates, unsafe paths and invalid new links; never work around that rejection with a renamed duplicate.

Preserve time and correction history. A later development gets a later event. Historical prose is not silently rewritten. correct_page appends a dated, source-linked correction while retaining the prior record. Do not confuse a changed opinion with a factual correction. Never infer a current state from old or absent evidence.

Human Wiki notes use type hub/event/insight/topic/archive, cssclasses: liva-wiki, exactly one Markdown H1, a matching frontmatter title, and a small number of meaningful hub/parent links. Never put a second copy of the title into the body. Events use a documented event_date or explicit unknown/approximate precision; never invent a date. Preserve provenance for every non-trivial claim.

For training/body questions, distinguish current structured state from durable knowledge before recalling memory: today's training or next session -> training decision/plan; current weight -> weight state; current nutrition -> nutrition data; current recovery -> recovery snapshot. Do not turn transient live values into Living Wiki facts.

User-facing wording must be short and factual. On verified knowledge success say what was updated and whether maintenance is clean. On source-only capture say: "Quelle erfasst; die Living Wiki wurde nicht geändert." On failure say that the change was not saved and give the concrete recoverable reason. Never expose staging jargon, claim IDs or internal transaction details unless the user is debugging the backend.

useMemos is a read-only rollback archive and never a normal write path. Full operational procedure: procedures/liva-memory-gpt-workflow.md. Full Gardener policy: procedures/skills/liva-memory-gardener/SKILL.md."""
