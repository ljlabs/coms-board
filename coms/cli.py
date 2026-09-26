"""coms — command line for agents to use the coms-board.

    python3 coms.py --as <agent-slug> <group> <command> [args]

Identity comes from --as or $COMS_AGENT. Output is JSON (machine-friendly)
unless --text is given, in which case a compact human summary is printed.
Talks to the SQLite database directly; the dashboard server is optional.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coms import api, db  # noqa: E402


def _read_body(args) -> str:
    """Body from --body, --file, or stdin ('-')."""
    if getattr(args, "file", None):
        if args.file == "-":
            return sys.stdin.read()
        return Path(args.file).read_text()
    return getattr(args, "body", None) or ""


def _out(args, data, text_fn=None):
    if args.text and text_fn:
        print(text_fn(data))
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))


def _short(s: str, n: int = 90) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _t_tickets(rows):
    if not rows:
        return "(no tickets)"
    return "\n".join(
        f"#{t['id']:<4} {t['priority']} {t['status']:<12} {t['type']:<9} "
        f"{(t['assignee'] or '-'):<28} {_short(t['title'], 70)}" for t in rows)


def _t_ticket(t):
    head = (f"#{t['id']} [{t['status']}/{t['priority']}/{t['type']}] {t['title']}\n"
            f"assignee={t['assignee'] or '-'} requester={t['requester'] or '-'} "
            f"dept={t['department'] or '-'} tags={t['tags'] or '-'}\n"
            f"created={t['created_at']} updated={t['updated_at']}\n")
    if t.get("parent"):
        p = t["parent"]
        head += f"parent: #{p['id']} [{p['type']}/{p['status']}] {_short(p['title'], 60)}\n"
    if t.get("children"):
        head += "children:\n" + "\n".join(
            f"  #{k['id']} [{k['type']}/{k['status']}] {(k['assignee'] or '-')}: {_short(k['title'], 55)}"
            for k in t["children"]) + "\n"
    if t.get("links"):
        head += "links:\n" + "\n".join(
            f"  {ln['kind']}:{ln['ref']}"
            + (f" — {_short(ln['title'], 55)}" if ln.get("title") else "")
            + (f" ({ln['note']})" if ln.get("note") else "")
            for ln in t["links"]) + "\n"
    head += f"\n{t['body']}"
    if t.get("result"):
        head += f"\n\n--- RESULT ---\n{t['result']}"
    if t.get("comments"):
        head += "\n\n--- COMMENTS ---\n" + "\n".join(
            f"[{c['created_at']}] {c['author']} ({c['kind']}): {c['body']}" for c in t["comments"])
    return head


def _t_wiki_list(rows):
    if not rows:
        return "(no pages)"
    return "\n".join(f"{p['slug']:<44} {p['updated_at']}  {_short(p['title'], 60)}  [{p['tags']}]"
                     f"  /{p.get('folder') or ''}" for p in rows)


def _t_wiki(p):
    return (f"# {p['title']}  ({p['slug']})\n"
            f"folder=/{p.get('folder') or ''} tags={p['tags'] or '-'} dept={p['department'] or '-'} "
            f"author={p['author']} "
            f"updated={p['updated_at']} by {p['updated_by']} revisions={p.get('revision_count', 0)}\n"
            f"backlinks={', '.join(b['slug'] for b in p.get('backlinks', [])) or '-'}\n\n{p['body']}")


def _t_wiki_tree(t):
    """Folder outline: indented folders with page counts, pages listed beneath each."""
    out = []
    root = t["pages"].get("", [])
    if not t["prefix"] and root:
        out.append(f"(unfiled)  {len(root)} pages")
        out += [f"  - {p['slug']}  — {_short(p['title'], 60)}" for p in root]
    for f in t["folders"]:
        ind = "  " * f["depth"]
        out.append(f"{ind}{f['name']}/  ({f['page_count']} here, {f['total_count']} total)")
        for p in t["pages"].get(f["path"], []):
            out.append(f"{ind}  - {p['slug']}  — {_short(p['title'], 60)}")
    return "\n".join(out) or "(no pages)"


def _t_search(rows):
    if not rows:
        return "(no matches)"
    out = []
    for r in rows:
        key = r.get("slug") or f"#{r.get('id') or r.get('question_id')}"
        out.append(f"{key:<44} {_short(r.get('title', ''), 60)}\n    {_short(r.get('snippet', ''), 160)}")
    return "\n".join(out)


def _t_questions(rows):
    if not rows:
        return "(no questions)"
    return "\n".join(
        f"Q#{q['id']:<4} score={q['score']:<3} answers={q['answer_count']:<2} {q['status']:<9} "
        f"{q['author']:<26} {_short(q['title'], 70)}  [{q['tags']}]" for q in rows)


def _t_question(q):
    s = (f"Q#{q['id']} [{q['status']}] score={q['score']} views={q['views']} by {q['author']} "
         f"tags={q['tags'] or '-'}\n# {q['title']}\n\n{q['body']}\n")
    for c in q.get("comments", []):
        s += f"  ↳ ({c['author']}) {c['body']}\n"
    for a in q.get("answers", []):
        s += (f"\n--- ANSWER #{a['id']} score={a['score']}{' ✔ ACCEPTED' if a['accepted'] else ''} "
              f"by {a['author']} at {a['created_at']} ---\n{a['body']}\n")
        for c in a.get("comments", []):
            s += f"  ↳ ({c['author']}) {c['body']}\n"
    return s


def _t_inbox(d):
    s = f"INBOX for {d['agent']} (dept={d['department'] or '-'})\n\nMY TICKETS:\n{_t_tickets(d['my_tickets'])}\n"
    if d.get("awaiting_my_review"):
        s += f"\n⚠️ AWAITING MY REVIEW (tickets you requested, expert handed back — judge + close):\n{_t_tickets(d['awaiting_my_review'])}\n"
    s += f"\nCLAIMABLE TICKETS:\n{_t_tickets(d['claimable_tickets'])}\n"
    s += f"\nUNANSWERED QUESTIONS:\n{_t_questions(d['unanswered_questions'])}\n"
    if d["my_questions_with_new_answers"]:
        s += f"\nMY QUESTIONS WITH ANSWERS:\n{_t_questions(d['my_questions_with_new_answers'])}\n"
    return s


def _t_tree(d):
    lines = []

    def walk(n, prefix=""):
        mark = " ◀" if n["id"] == d["focus"] else ""
        lines.append(f"{prefix}#{n['id']} [{n['type']}/{n['status']}] "
                     f"{(n['assignee'] or '-')}: {_short(n['title'], 60)}{mark}")
        for c in n["children"]:
            walk(c, prefix + "  ")

    walk(d["root"])
    return "\n".join(lines)


def _t_ticket_overview(d):
    st = " ".join(f"{k}={v}" for k, v in sorted(d["by_status"].items()))
    s = (f"INVESTIGATION around ticket #{d['focus']} — {d['tickets_total']} tickets "
         f"({d['open_total']} open: {st})\n\n"
         + _t_tree({"focus": d["focus"], "root": d["tree"]}))
    if d.get("recent_activity"):
        s += "\n\nRECENT ACTIVITY:\n" + "\n".join(
            f"{a['created_at']} {a['actor']:<26} {a['summary']}" for a in d["recent_activity"])
    return s


# (group, cmd) pairs that never write — they get a read-only DB connection,
# so they work without write permission and create no -wal/-shm files.
READONLY_CMDS = {
    ("wiki", "search"), ("wiki", "list"), ("wiki", "get"), ("wiki", "history"), ("wiki", "tree"),
    ("ticket", "list"), ("ticket", "mine"), ("ticket", "get"), ("ticket", "search"),
    ("ticket", "tree"), ("ticket", "children"),
    ("questions", "list"), ("questions", "get"), ("questions", "search"),
    ("agent", "list"), ("agent", "get"), ("dept", "list"),
    ("search", None), ("inbox", None), ("activity", None), ("overview", None),
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="coms", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--as", dest="agent", default=os.environ.get("COMS_AGENT", ""),
                   help="acting agent slug (or $COMS_AGENT)")
    p.add_argument("--db", default=None, help="path to sqlite db (or $COMS_DB)")
    p.add_argument("--text", action="store_true", help="human-readable output instead of JSON")
    sub = p.add_subparsers(dest="group", required=True)

    # ---- wiki
    w = sub.add_parser("wiki", help="long-term agent notes").add_subparsers(dest="cmd", required=True)
    x = w.add_parser("search"); x.add_argument("query"); x.add_argument("--limit", type=int, default=20)
    x = w.add_parser("list"); x.add_argument("--dept"); x.add_argument("--tag")
    x.add_argument("--folder", help="only pages in this folder ('' or '/' = unfiled root)")
    x.add_argument("-r", "--recursive", action="store_true", help="with --folder: include sub-folders")
    x = w.add_parser("get"); x.add_argument("slug")
    x = w.add_parser("tree", help="folder outline of the wiki (folders + pages)")
    x.add_argument("prefix", nargs="?", default="", help="only this folder subtree")
    x = w.add_parser("put", help="create or fully replace a page")
    x.add_argument("slug"); x.add_argument("--title"); x.add_argument("--body"); x.add_argument("--file")
    x.add_argument("--tags"); x.add_argument("--dept")
    x.add_argument("--folder", help="file the page here, e.g. folder-a/folder-b/folder-c")
    x = w.add_parser("append", help="append markdown to an existing page (or create it)")
    x.add_argument("slug"); x.add_argument("--title"); x.add_argument("--body"); x.add_argument("--file")
    x.add_argument("--tags"); x.add_argument("--dept")
    x.add_argument("--folder", help="(re)file the page here")
    x = w.add_parser("mv", help="move a page to another folder (slug and links unchanged)")
    x.add_argument("slug"); x.add_argument("folder", help="target folder path; '/' or '' = unfiled")
    x = w.add_parser("delete"); x.add_argument("slug")
    x = w.add_parser("history"); x.add_argument("slug")

    # ---- tickets
    t = sub.add_parser("ticket", help="ticket queue").add_subparsers(dest="cmd", required=True)
    x = t.add_parser("list"); x.add_argument("--status", action="append"); x.add_argument("--assignee")
    x.add_argument("--dept"); x.add_argument("--type"); x.add_argument("--all", action="store_true")
    x.add_argument("--unassigned", action="store_true")
    x.add_argument("--parent", type=int, help="only direct children of this ticket")
    x = t.add_parser("mine")
    x = t.add_parser("get"); x.add_argument("id", type=int)
    x = t.add_parser("tree", help="whole hierarchy containing a ticket (story > job > tasks)")
    x.add_argument("id", type=int)
    x = t.add_parser("children", help="direct children of a ticket"); x.add_argument("id", type=int)
    x.add_argument("--all", action="store_true", help="include closed children")
    x = t.add_parser("create"); x.add_argument("title"); x.add_argument("--body"); x.add_argument("--file")
    x.add_argument("--type", default="task", choices=api.TICKET_TYPES)
    x.add_argument("--priority", default="p2", choices=api.PRIORITIES)
    x.add_argument("--dept", default=""); x.add_argument("--assignee", default="")
    x.add_argument("--tags"); x.add_argument("--due")
    x.add_argument("--parent", type=int, help="parent ticket id (story > job > task)")
    x = t.add_parser("claim"); x.add_argument("id", type=int)
    x = t.add_parser("update"); x.add_argument("id", type=int)
    x.add_argument("--status", choices=api.TICKET_STATUSES); x.add_argument("--priority", choices=api.PRIORITIES)
    x.add_argument("--assignee"); x.add_argument("--title"); x.add_argument("--body"); x.add_argument("--tags")
    x.add_argument("--dept"); x.add_argument("--type", choices=api.TICKET_TYPES)
    x.add_argument("--parent", help="parent ticket id, or 'none' to detach")
    x = t.add_parser("comment"); x.add_argument("id", type=int); x.add_argument("--body"); x.add_argument("--file")
    x = t.add_parser("link", help="attach context: wiki page, question, ticket, or url")
    x.add_argument("id", type=int); x.add_argument("kind", choices=["wiki", "question", "ticket", "url"])
    x.add_argument("ref", help="wiki slug, question id, ticket id, or url"); x.add_argument("--note", default="")
    x = t.add_parser("unlink"); x.add_argument("id", type=int)
    x.add_argument("kind", choices=["wiki", "question", "ticket", "url"]); x.add_argument("ref")
    x = t.add_parser("done", help="record the result and close the ticket")
    x.add_argument("id", type=int); x.add_argument("--result"); x.add_argument("--file")
    x = t.add_parser("search"); x.add_argument("query")

    # ---- questions (stack-exchange style Q&A)
    b = sub.add_parser("questions",
                       help="stack-exchange style Q&A").add_subparsers(dest="cmd", required=True)
    x = b.add_parser("list"); x.add_argument("--status", choices=api.QUESTION_STATUSES)
    x.add_argument("--tag"); x.add_argument("--dept"); x.add_argument("--unanswered", action="store_true")
    x = b.add_parser("get"); x.add_argument("id", type=int)
    x = b.add_parser("ask"); x.add_argument("title"); x.add_argument("--body"); x.add_argument("--file")
    x.add_argument("--tags"); x.add_argument("--dept", default="")
    x = b.add_parser("answer"); x.add_argument("id", type=int); x.add_argument("--body"); x.add_argument("--file")
    x = b.add_parser("edit-answer"); x.add_argument("answer_id", type=int); x.add_argument("--body"); x.add_argument("--file")
    x = b.add_parser("vote"); x.add_argument("target", choices=["question", "answer"])
    x.add_argument("id", type=int); x.add_argument("value", type=int, choices=[-1, 0, 1])
    x = b.add_parser("accept"); x.add_argument("question_id", type=int); x.add_argument("answer_id", type=int)
    x = b.add_parser("comment"); x.add_argument("target", choices=["question", "answer"])
    x.add_argument("id", type=int); x.add_argument("--body"); x.add_argument("--file")
    x = b.add_parser("close"); x.add_argument("id", type=int)
    x = b.add_parser("search"); x.add_argument("query")

    # ---- agents / departments / misc
    a = sub.add_parser("agent", help="agent registry").add_subparsers(dest="cmd", required=True)
    x = a.add_parser("list"); x.add_argument("--dept")
    x = a.add_parser("get"); x.add_argument("slug")
    x = a.add_parser("register"); x.add_argument("slug"); x.add_argument("--name")
    x.add_argument("--dept", default=""); x.add_argument("--role", default="expert")
    x.add_argument("--description", default=""); x.add_argument("--codebases", default="")
    x.add_argument("--wiki-pages", default=""); x.add_argument("--agent-file", default="")
    d = sub.add_parser("dept", help="departments").add_subparsers(dest="cmd", required=True)
    x = d.add_parser("list")
    x = d.add_parser("register"); x.add_argument("slug"); x.add_argument("--name"); x.add_argument("--description", default="")

    x = sub.add_parser("search", help="search wiki + tickets + questions"); x.add_argument("query")
    x = sub.add_parser("inbox", help="what should I look at? (my tickets, claimable, unanswered)")
    x = sub.add_parser("activity"); x.add_argument("--limit", type=int, default=30); x.add_argument("--actor")
    x = sub.add_parser("overview")
    x.add_argument("--ticket", type=int, help="scope to one investigation: the ticket's tree + its activity")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    g, c = args.group, getattr(args, "cmd", None)
    ro = (g, c) in READONLY_CMDS or (g, None) in READONLY_CMDS
    conn = db.connect(args.db, readonly=ro) if args.db else db.connect(readonly=ro)
    me = args.agent
    try:
        if g == "wiki":
            if c == "search":
                _out(args, api.search_wiki(conn, args.query, args.limit), _t_search)
            elif c == "list":
                folder = args.folder
                if folder is not None and folder.strip() in ("/", ""):
                    folder = ""
                _out(args, api.list_wiki(conn, args.dept, args.tag, folder=folder,
                                         recursive=args.recursive), _t_wiki_list)
            elif c == "get":
                _out(args, api.get_wiki(conn, args.slug), _t_wiki)
            elif c == "tree":
                _out(args, api.wiki_tree(conn, args.prefix), _t_wiki_tree)
            elif c in ("put", "append"):
                _need_identity(me)
                _out(args, api.save_wiki(conn, args.slug, args.title or "", _read_body(args), me,
                                         tags=args.tags, department=args.dept, append=(c == "append"),
                                         folder=args.folder),
                     lambda p: f"saved {p['slug']} ({len(p['body'])} chars) in /{p.get('folder') or ''}")
            elif c == "mv":
                _need_identity(me)
                target = "" if args.folder.strip() in ("/", "") else args.folder
                _out(args, api.move_wiki(conn, args.slug, target, me),
                     lambda p: f"moved {p['slug']} -> /{p.get('folder') or ''}")
            elif c == "delete":
                _need_identity(me)
                api.delete_wiki(conn, args.slug, me)
                _out(args, {"deleted": args.slug}, lambda d: f"deleted {d['deleted']}")
            elif c == "history":
                _out(args, api.wiki_revisions(conn, args.slug))
        elif g == "ticket":
            if c == "list":
                assignee = "" if args.unassigned else args.assignee
                _out(
                    args,
                    api.list_tickets(
                        conn,
                        status=args.status,
                        assignee=assignee,
                        department=args.dept,
                        type_=args.type,
                        include_closed=args.all,
                        parent_id=args.parent,
                    ),
                    _t_tickets,
                )
            elif c == "mine":
                _need_identity(me)
                _out(args, api.list_tickets(conn, assignee=me), _t_tickets)
            elif c == "get":
                _out(args, api.get_ticket(conn, args.id), _t_ticket)
            elif c == "tree":
                _out(args, api.ticket_tree(conn, args.id), _t_tree)
            elif c == "children":
                _out(args, api.list_tickets(conn, parent_id=args.id, include_closed=args.all),
                     _t_tickets)
            elif c == "create":
                _need_identity(me)
                _out(args, api.create_ticket(conn, args.title, _read_body(args), me, args.type,
                                             args.priority, args.dept, args.assignee, args.tags,
                                             args.due, parent_id=args.parent),
                     _t_ticket)
            elif c == "claim":
                _need_identity(me)
                _out(args, api.claim_ticket(conn, args.id, me), _t_ticket)
            elif c == "update":
                _need_identity(me)
                t = api.update_ticket(conn, args.id, me, status=args.status, priority=args.priority,
                                      assignee=args.assignee, title=args.title, body=args.body,
                                      tags=args.tags, department=args.dept, type=args.type,
                                      parent_id=args.parent)
                _warn_open_children(t)
                _out(args, t, _t_ticket)
            elif c == "comment":
                _need_identity(me)
                _out(args, api.comment_ticket(conn, args.id, me, _read_body(args)), _t_ticket)
            elif c == "link":
                _need_identity(me)
                _out(args, api.link_ticket(conn, args.id, args.kind, args.ref, me, args.note), _t_ticket)
            elif c == "unlink":
                _need_identity(me)
                _out(args, api.unlink_ticket(conn, args.id, args.kind, args.ref, me), _t_ticket)
            elif c == "done":
                _need_identity(me)
                result = args.result or (Path(args.file).read_text() if args.file and args.file != "-"
                                         else (sys.stdin.read() if args.file == "-" else ""))
                t = api.update_ticket(conn, args.id, me, status="done", result=result)
                _warn_open_children(t)
                _out(args, t, _t_ticket)
            elif c == "search":
                _out(args, api.search_tickets(conn, args.query), _t_tickets)
        elif g == "questions":
            if c == "list":
                _out(args, api.list_questions(conn, args.status, args.tag, args.dept, args.unanswered), _t_questions)
            elif c == "get":
                _out(args, api.get_question(conn, args.id, me), _t_question)
            elif c == "ask":
                _need_identity(me)
                _out(args, api.ask_question(conn, args.title, _read_body(args), me, args.tags, args.dept), _t_question)
            elif c == "answer":
                _need_identity(me)
                _out(args, api.answer_question(conn, args.id, me, _read_body(args)), _t_question)
            elif c == "edit-answer":
                _need_identity(me)
                _out(args, api.edit_answer(conn, args.answer_id, me, _read_body(args)), _t_question)
            elif c == "vote":
                _need_identity(me)
                _out(args, api.vote(conn, args.target, args.id, me, args.value),
                     lambda v: f"{v['target_type']} #{v['target_id']} score={v['score']}")
            elif c == "accept":
                _need_identity(me)
                _out(args, api.accept_answer(conn, args.question_id, args.answer_id, me), _t_question)
            elif c == "comment":
                _need_identity(me)
                _out(args, api.comment_question(conn, args.target, args.id, me, _read_body(args)), _t_question)
            elif c == "close":
                _need_identity(me)
                _out(args, api.update_question(conn, args.id, me, status="closed"), _t_question)
            elif c == "search":
                _out(args, api.search_questions(conn, args.query), _t_search)
        elif g == "agent":
            if c == "list":
                _out(args, api.list_agents(conn, args.dept),
                     lambda rows: "\n".join(f"{a['slug']:<34} {a['role']:<8} {a['department']:<14} rep={a['reputation']:<4} "
                                            f"open={a['open_tickets']}  {_short(a['description'], 60)}" for a in rows))
            elif c == "get":
                _out(args, api.get_agent(conn, args.slug))
            elif c == "register":
                _out(args, api.upsert_agent(conn, args.slug, args.name or args.slug, department=args.dept,
                                            role=args.role, description=args.description, codebases=args.codebases,
                                            wiki_pages=args.wiki_pages, agent_file=args.agent_file))
        elif g == "dept":
            if c == "list":
                _out(args, api.list_departments(conn))
            elif c == "register":
                _out(args, api.upsert_department(conn, args.slug, args.name or args.slug, args.description))
        elif g == "search":
            r = api.search_all(conn, args.query)
            _out(args, r, lambda r: f"WIKI:\n{_t_search(r['wiki'])}\n\nTICKETS:\n{_t_tickets(r['tickets'])}\n\nQUESTIONS:\n{_t_search(r['questions'])}")
        elif g == "inbox":
            _need_identity(me)
            _out(args, api.inbox(conn, me), _t_inbox)
        elif g == "activity":
            _out(args, api.activity(conn, args.limit, args.actor),
                 lambda rows: "\n".join(f"{a['created_at']} {a['actor']:<26} {a['summary']}" for a in rows))
        elif g == "overview":
            if getattr(args, "ticket", None):
                _out(args, api.ticket_overview(conn, args.ticket), _t_ticket_overview)
            else:
                _out(args, api.overview(conn))
        return 0
    except (api.NotFound, api.BadRequest) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 2


def _warn_open_children(t: dict) -> None:
    kids = t.get("open_children_warning")
    if kids:
        print(f"⚠️  ticket #{t['id']} closed with {len(kids)} open child ticket(s): "
              + ", ".join(f"#{k['id']} ({k['status']})" for k in kids), file=sys.stderr)


def _need_identity(me: str) -> None:
    if not me:
        raise api.BadRequest("acting identity required: pass --as <agent-slug> or set $COMS_AGENT")


if __name__ == "__main__":
    sys.exit(main())
