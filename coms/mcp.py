"""Coms Board MCP tools over newline-delimited stdio (no dependencies).

Launched by the ACP backend with an absolute DB path and acting identity.
Never writes to stdout except for JSON-RPC frames. No filesystem tools.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coms import api, db


def tool(name, description, properties=None, required=(), write=False):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {},
                            "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": not write, "destructiveHint": False}}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
TOOLS = [
    tool("board_search", "Search existing wiki pages, tickets and questions first.", {"query": S}, ["query"]),
    tool("wiki_list", "List wiki pages."),
    tool("wiki_get", "Read a wiki page.", {"slug": S}, ["slug"]),
    tool("wiki_put", "Publish durable knowledge to the board wiki; replaces the page body.",
         {"slug": S, "title": S, "body": S, "folder": S, "tags": S}, ["slug", "title", "body"], True),
    tool("wiki_append", "Append knowledge to a board wiki page.", {"slug": S, "body": S}, ["slug", "body"], True),
    tool("ticket_list", "List tickets, including completed tickets."),
    tool("ticket_get", "Read a ticket, its result, comments and links.", {"id": I}, ["id"]),
    tool("ticket_create", "Create prioritized work. Set ready only for actionable agent work.",
         {"title": S, "body": S, "priority": {"type": "string", "enum": list(api.PRIORITIES)},
          "assignee": S, "tags": S, "parent_id": I, "template_id": I, "ready": B,
          "type": {"type": "string", "enum": list(api.TICKET_TYPES)}, "recommended_workers": I}, ["title"], True),
    tool("ticket_comment", "Publish a concise durable update to a ticket.", {"id": I, "body": S}, ["id", "body"], True),
    tool("ticket_link", "Attach a wiki page, question, ticket or URL to a ticket.",
         {"id": I, "kind": {"type": "string", "enum": ["wiki", "question", "ticket", "url"]},
          "ref": S, "note": S}, ["id", "kind", "ref"], True),
    tool("ticket_result", "Required before finishing execution: record deliverables, board IDs/links and verification. Runner sends work for review.",
         {"id": I, "result": S}, ["id", "result"], True),
    tool("questions_list", "List questions."),
    tool("questions_get", "Read a question and its answers.", {"id": I}, ["id"]),
    tool("questions_ask", "Publish an unresolved question to the board.", {"title": S, "body": S}, ["title", "body"], True),
    tool("questions_answer", "Answer an existing question on the board.", {"id": I, "body": S}, ["id", "body"], True),
    tool("agent_templates", "List available execution templates before creating work."),
    tool("agents_list", "List registered agents and their capabilities. Prefer reusing a suitable teammate."),
    tool("agent_ensure", "Reuse a matching registered agent, or create a named teammate if none fits. An explicit slug selects that identity.",
         {"name": S, "slug": S, "role": S, "description": S, "instructions": S}, ["name"], True),
    tool("skill_list", "List shared procedural skills available to all agents."),
    tool("skill_get", "Read a shared skill and its current version.", {"slug": S}, ["slug"]),
    tool("skill_put", "Publish a verified reusable procedure for all agents. For updates, pass the version returned by skill_get.",
         {"slug": S, "title": S, "body": S, "version": I}, ["slug", "title", "body"], True),
]


def validate(spec, arguments):
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    schema = spec["inputSchema"]
    for key in schema["required"]:
        if key not in arguments:
            raise ValueError(f"missing argument: {key}")
    for key, value in arguments.items():
        prop = schema["properties"].get(key)
        if prop is None:
            raise ValueError(f"unknown argument: {key}")
        kind = prop["type"]
        valid = (isinstance(value, str) if kind == "string" else
                 type(value) is int if kind == "integer" else type(value) is bool)
        if not valid or ("enum" in prop and value not in prop["enum"]):
            raise ValueError(f"invalid argument: {key}")


def call_tool(name, args, *, readonly=False):
    spec = next((t for t in TOOLS if t["name"] == name), None)
    if not spec or (readonly and not spec["annotations"]["readOnlyHint"]):
        raise ValueError(f"tool unavailable: {name}")
    validate(spec, args)
    actor = os.environ.get("COMS_AGENT", "agent-runner")
    assigned = os.environ.get("COMS_TICKET_ID")
    if assigned and name in ("ticket_result", "ticket_comment", "ticket_link") and args["id"] != int(assigned):
        raise ValueError("execution agent may only change its assigned ticket")
    conn = db.connect(readonly=readonly)
    if name == "board_search": result = api.search_all(conn, args["query"])
    elif name == "wiki_list": result = api.list_wiki(conn)
    elif name == "wiki_get": result = api.get_wiki(conn, args["slug"])
    elif name in ("wiki_put", "wiki_append"):
        result = api.save_wiki(conn, args["slug"], args.get("title", ""), args["body"], actor,
                               tags=args.get("tags"), folder=args.get("folder"), append=name == "wiki_append")
    elif name == "ticket_list": result = api.list_tickets(conn, include_closed=True)
    elif name == "ticket_get": result = api.get_ticket(conn, args["id"])
    elif name == "ticket_create":
        result = api.create_ticket(conn, args["title"], args.get("body", ""), actor,
                                   type_=args.get('type', 'task'), priority=args.get("priority", "p2"), assignee=args.get("assignee", ""),
                                   tags=args.get("tags"), parent_id=args.get("parent_id"),
                                   ready_for_execution=args.get("ready", False), agent_template_id=args.get("template_id"), recommended_workers=args.get('recommended_workers'))
        if args.get('ready'):
            api.resolve_ticket_agent(conn, result)
            result = api.get_ticket(conn, result['id'])
    elif name == "ticket_comment": result = api.comment_ticket(conn, args["id"], actor, args["body"])
    elif name == "ticket_link": result = api.link_ticket(conn, args["id"], args["kind"], args["ref"], actor, args.get("note", ""))
    elif name == "ticket_result":
        if not args["result"].strip(): raise ValueError("a nonempty result is required")
        result = api.update_ticket(conn, args["id"], actor, result=args["result"])
    elif name == "questions_list": result = api.list_questions(conn)
    elif name == "questions_get": result = api.get_question(conn, args["id"], actor)
    elif name == "questions_ask": result = api.ask_question(conn, args["title"], args["body"], actor)
    elif name == "questions_answer": result = api.answer_question(conn, args["id"], actor, args["body"])
    elif name == "agent_templates": result = [dict(r) for r in conn.execute("SELECT id,name,backend,enabled FROM agent_templates ORDER BY name")]
    elif name == "agents_list": result = api.list_agents(conn)
    elif name == "agent_ensure": result = api.ensure_agent(conn, args['name'], args.get('role', 'expert'), args.get('description', ''), args.get('slug', ''), args.get('instructions', ''))
    elif name == "skill_list": result = api.list_skills(conn)
    elif name == "skill_get": result = api.get_skill(conn, args['slug'])
    elif name == "skill_put": result = api.save_skill(conn, args['slug'], args['title'], args['body'], actor, args.get('version'))
    else: raise ValueError(f"tool unavailable: {name}")
    if not spec["annotations"]["readOnlyHint"] and os.environ.get("COMS_RUN_ID"):
        conn.execute("INSERT INTO agent_messages(ticket_id,run_id,role,kind,body,created_at) VALUES(?,?,'system','board-delivery',?,?)",
                     (int(os.environ["COMS_TICKET_ID"]), int(os.environ["COMS_RUN_ID"]),
                      json.dumps({"tool": name, "id": result.get("id") if isinstance(result, dict) else None, "slug": result.get("slug") if isinstance(result, dict) else None}), api.now()))
        conn.commit()
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


def dispatch(message, readonly=False):
    mid = message.get("id")
    if mid is None: return None
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        versions = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
        requested = params.get("protocolVersion")
        result = {"protocolVersion": requested if requested in versions else "2025-11-25",
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "coms-board", "version": "1.0.0"}}
    elif method == "ping": result = {}
    elif method == "tools/list":
        result = {"tools": [t for t in TOOLS if not readonly or t["annotations"]["readOnlyHint"]]}
    elif method == "tools/call":
        try: result = call_tool(params.get("name"), params.get("arguments", {}), readonly=readonly)
        except (ValueError, api.BadRequest, api.NotFound) as exc:
            result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readonly", action="store_true")
    args = parser.parse_args()
    try:
        for line in sys.stdin:
            message = None
            try:
                message = json.loads(line)
                if not isinstance(message, dict): raise ValueError("request must be an object")
                response = dispatch(message, args.readonly)
            except Exception as exc:
                response = {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                            "error": {"code": -32603, "message": str(exc)}}
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
    finally:
        conn = getattr(db._local, "conn", None)
        if conn: conn.close()


if __name__ == "__main__": main()
