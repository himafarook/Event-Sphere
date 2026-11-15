"""Message bus + Agent base class.

Agents never import or call each other. They only call `bus.send(...)`, which creates a
JSON-style message (sender, receiver, performative, content, conversation_id), logs it,
delivers it to the receiver's `handle()` and logs the reply.
"""
import json
import uuid
from ..models import MessageLog, AgentState
from ..config import now

PERFORMATIVES = {"REQUEST", "AGREE", "REFUSE", "PROPOSE", "INFORM", "FAILURE"}


class Bus:
    def __init__(self):
        self.agents = {}

    def register(self, agent):
        self.agents[agent.name] = agent

    def _log(self, db, m):
        db.add(MessageLog(ts=now(), conv_id=m["conversation_id"], sender=m["sender"],
                          receiver=m["receiver"], performative=m["performative"],
                          content=json.dumps(m["content"], default=str)))
        db.flush()

    def send(self, db, sender, receiver, performative, content, conversation_id=None):
        if performative not in PERFORMATIVES:
            raise ValueError(f"bad performative {performative}")
        conv = conversation_id or uuid.uuid4().hex[:12]
        msg = {"sender": sender, "receiver": receiver, "performative": performative,
               "content": content, "conversation_id": conv}
        self._log(db, msg)
        agent = self.agents.get(receiver)
        if agent is None:
            out = ("FAILURE", {"reason": f"unknown agent {receiver}"})
        else:
            try:
                out = agent.handle(db, msg)
            except Exception as exc:  # an agent failure becomes a FAILURE message
                db.rollback()
                out = ("FAILURE", {"reason": f"{type(exc).__name__}: {exc}"})
        if out is None:
            return None
        reply = {"sender": receiver, "receiver": sender, "performative": out[0],
                 "content": out[1], "conversation_id": conv}
        self._log(db, reply)
        return reply


bus = Bus()


class Agent:
    name = "Agent"

    def __init__(self):
        bus.register(self)

    # ---- persistent beliefs -------------------------------------------------
    def get(self, db, key, default=0):
        row = db.get(AgentState, (self.name, key))
        return json.loads(row.value) if row else default

    def put(self, db, key, value):
        row = db.get(AgentState, (self.name, key))
        if row:
            row.value = json.dumps(value)
        else:
            db.add(AgentState(agent=self.name, key=key, value=json.dumps(value)))
        db.flush()

    def incr(self, db, key, n=1):
        self.put(db, key, self.get(db, key, 0) + n)

    # ---- messaging ----------------------------------------------------------
    def send(self, db, to, perf, content, conv=None):
        return bus.send(db, self.name, to, perf, content, conv)

    def handle(self, db, msg):
        c = msg["content"]
        action = c.get("action") or c.get("event")
        fn = getattr(self, f"on_{action}", None)
        if fn is None:
            return ("REFUSE", {"reason": f"{self.name} does not understand '{action}'"})
        return fn(db, c, msg)
