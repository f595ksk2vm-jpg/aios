"""Model management: provider registry, catalog, routing and fallback."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from ..db import Database, new_id
from ..errors import AiosError, Conflict, NotFound
from ..models import ModelEntry, ModelProvider
from .anthropic_provider import AnthropicProvider
from .openai_compat import OpenAICompatProvider
from .scripted import ScriptedProvider
from .types import ChatResult, ProviderError, ToolSpec

log = logging.getLogger("aios.llm")

PROVIDER_KINDS = {"anthropic": AnthropicProvider, "openai_compat": OpenAICompatProvider, "scripted": ScriptedProvider}
TIER_QUALITY = {"fast": 1, "balanced": 2, "deep": 3}
# (quality, cost, latency) weights per routing preference.
PREFERENCES = {
    "quality": (1.0, 0.1, 0.1),
    "balanced": (0.6, 0.3, 0.2),
    "cost": (0.25, 1.0, 0.1),
    "latency": (0.3, 0.2, 1.0),
}

# Seeded on first start. Prices are USD per million tokens.
DEFAULT_CATALOG = [
    {"provider": {"name": "anthropic", "kind": "anthropic", "location": "cloud", "secret_name": "ANTHROPIC_API_KEY", "enabled": True},
     "models": [
         {"model_name": "claude-opus-5-5", "display_name": "Claude Opus 5.5", "tier": "deep", "context_window": 1_000_000,
          "price_in": 4.0, "price_out": 20.0, "latency_class": 3, "meta": {"effort": "high"}},
         {"model_name": "claude-sonnet-5-5", "display_name": "Claude Sonnet 5.5", "tier": "balanced", "context_window": 1_000_000,
          "price_in": 2.0, "price_out": 10.0, "latency_class": 2},
         {"model_name": "claude-haiku-5-5", "display_name": "Claude Haiku 5.5", "tier": "fast", "context_window": 1_000_000,
          "price_in": 0.1, "price_out": 0.5, "latency_class": 1},
     ]},
    # A local OpenAI-compatible endpoint (Ollama's default address). Disabled until
    # the operator enables it and adds or discovers the models actually installed.
    {"provider": {"name": "local", "kind": "openai_compat", "location": "local", "base_url": "http://localhost:11434/v1", "enabled": False},
     "models": []},
]


@dataclass
class Candidate:
    provider: ModelProvider
    entry: ModelEntry

    @property
    def ref(self) -> str:
        return f"{self.provider.name}/{self.entry.model_name}"


class ModelService:
    def __init__(self, db: Database, secrets, events, enable_scripted: bool = False):
        self.db = db
        self.secrets = secrets
        self.events = events
        self.enable_scripted = enable_scripted
        self._clients: dict[str, tuple[tuple, object]] = {}
        self._cooldown: dict[str, float] = {}  # provider name -> monotonic time until which it is skipped

    # --- catalog -----------------------------------------------------------------

    async def seed(self) -> None:
        async with self.db.session() as s:
            if (await s.execute(select(ModelProvider).limit(1))).first():
                return
        for item in DEFAULT_CATALOG:
            p = await self.upsert_provider(**item["provider"])
            for m in item["models"]:
                await self.upsert_model(provider_id=p.id, **m)
        if self.enable_scripted:
            p = await self.upsert_provider(name="scripted", kind="scripted", location="local")
            await self.upsert_model(provider_id=p.id, model_name="scripted-1", display_name="Scripted (dev only)", tier="balanced")

    async def providers(self) -> list[ModelProvider]:
        async with self.db.session() as s:
            return list((await s.execute(select(ModelProvider).order_by(ModelProvider.name))).scalars())

    async def catalog(self) -> list[ModelEntry]:
        async with self.db.session() as s:
            return list((await s.execute(select(ModelEntry).order_by(ModelEntry.provider_id, ModelEntry.model_name))).scalars())

    async def upsert_provider(self, *, name: str, kind: str, location: str = "cloud", base_url: str | None = None,
                              secret_name: str | None = None, enabled: bool = True, meta: dict | None = None) -> ModelProvider:
        if kind not in PROVIDER_KINDS:
            raise Conflict(f"provider kind must be one of {sorted(PROVIDER_KINDS)}")
        if kind == "scripted" and not self.enable_scripted:
            raise Conflict("the scripted provider is disabled (set AIOS_ENABLE_SCRIPTED_PROVIDER for development)")
        if location not in {"cloud", "local"}:
            raise Conflict("location must be cloud|local")
        if kind == "openai_compat" and not base_url:
            raise Conflict("openai_compat providers need a base_url")
        async with self.db.session() as s:
            p = (await s.execute(select(ModelProvider).where(ModelProvider.name == name))).scalar_one_or_none()
            if p:
                p.kind, p.location, p.base_url, p.secret_name, p.enabled = kind, location, base_url, secret_name, enabled
                if meta is not None:
                    p.meta = meta
            else:
                p = ModelProvider(id=new_id("prv"), name=name, kind=kind, location=location, base_url=base_url,
                                  secret_name=secret_name, enabled=enabled, meta=meta or {})
                s.add(p)
        self._clients.pop(name, None)
        return p

    async def delete_provider(self, provider_id: str) -> None:
        async with self.db.session() as s:
            p = await s.get(ModelProvider, provider_id)
            if not p:
                raise NotFound("provider not found")
            for m in (await s.execute(select(ModelEntry).where(ModelEntry.provider_id == provider_id))).scalars():
                await s.delete(m)
            await s.delete(p)

    async def upsert_model(self, *, provider_id: str, model_name: str, **fields) -> ModelEntry:
        if fields.get("tier", "balanced") not in TIER_QUALITY:
            raise Conflict("tier must be fast|balanced|deep")
        async with self.db.session() as s:
            if not await s.get(ModelProvider, provider_id):
                raise NotFound("provider not found")
            m = (await s.execute(select(ModelEntry).where(ModelEntry.provider_id == provider_id,
                                                          ModelEntry.model_name == model_name))).scalar_one_or_none()
            if not m:
                m = ModelEntry(id=new_id("mdl"), provider_id=provider_id, model_name=model_name)
                s.add(m)
            for k, v in fields.items():
                if v is not None and hasattr(m, k):
                    setattr(m, k, v)
            m.display_name = m.display_name or model_name
        return m

    async def delete_model(self, model_id: str) -> None:
        async with self.db.session() as s:
            m = await s.get(ModelEntry, model_id)
            if not m:
                raise NotFound("model not found")
            await s.delete(m)

    # --- routing -----------------------------------------------------------------

    async def candidates(self, model_cfg: dict | None, *, models_placement: str = "any", needs_tools: bool = True) -> list[Candidate]:
        """Ordered list of models that satisfy the constraints; the caller falls back down the list.

        ``model_cfg`` is the agent's model configuration:
        ``{"mode":"pinned","model":"provider/name"}`` or
        ``{"mode":"auto","tier":"balanced","prefer":"balanced"}``.
        ``models_placement`` is the namespace's data-residency rule: ``local``
        means prompts may only go to locally hosted models.
        """
        cfg = model_cfg or {}
        provs = {p.id: p for p in await self.providers()}
        pool = [Candidate(provs[m.provider_id], m) for m in await self.catalog()
                if m.enabled and provs[m.provider_id].enabled
                and (not needs_tools or m.supports_tools)
                and (models_placement == "any" or provs[m.provider_id].location == models_placement)]
        if not pool:
            return []
        want = TIER_QUALITY.get(cfg.get("tier", "balanced"), 2)
        wq, wc, wl = PREFERENCES.get(cfg.get("prefer", "balanced"), PREFERENCES["balanced"])
        max_price = max((c.entry.price_in + 3 * c.entry.price_out) for c in pool) or 1.0
        now = time.monotonic()

        def score(c: Candidate) -> float:
            q = TIER_QUALITY.get(c.entry.tier, 2)
            # Quality counts up to the requested tier; a model below it is penalised hard.
            quality = min(q, want) / want - (0.75 if q < want else 0.0)
            cost = (c.entry.price_in + 3 * c.entry.price_out) / max_price
            latency = (c.entry.latency_class - 1) / 2
            cooling = 5.0 if self._cooldown.get(c.provider.name, 0) > now else 0.0
            return wq * quality - wc * cost - wl * latency - cooling

        ranked = sorted(pool, key=score, reverse=True)
        if cfg.get("mode") == "pinned" and cfg.get("model"):
            pinned = [c for c in ranked if c.ref == cfg["model"]]
            # A pin is a preference, not a way around placement: if the pinned
            # model is not eligible it simply isn't in the pool.
            ranked = pinned + [c for c in ranked if c.ref != cfg["model"]]
        return ranked

    async def _client(self, p: ModelProvider, namespace_chain: list[str]):
        api_key = await self.secrets.get(p.secret_name, namespace_chain) if p.secret_name else None
        sig = (p.kind, p.base_url, api_key, str(p.meta))
        cached = self._clients.get(p.name)
        if cached and cached[0] == sig:
            return cached[1]
        if p.kind == "anthropic":
            client = AnthropicProvider(api_key=api_key, base_url=p.base_url, meta=p.meta)
        elif p.kind == "openai_compat":
            client = OpenAICompatProvider(base_url=p.base_url or "", api_key=api_key, meta=p.meta)
        else:
            client = ScriptedProvider()
        self._clients[p.name] = (sig, client)
        return client

    async def chat(self, candidates: list[Candidate], *, system: str, messages: list[dict], tools: list[ToolSpec],
                   namespace_chain: list[str], max_tokens: int = 16000, effort: str | None = None,
                   max_fallbacks: int = 2) -> tuple[ChatResult, Candidate, int]:
        """Call the first healthy candidate, falling back on retryable failures.

        Returns ``(result, candidate_used, latency_ms)``.
        """
        if not candidates:
            raise AiosError("no enabled model satisfies this agent's model configuration and the namespace's placement policy")
        last: Exception | None = None
        for cand in candidates[: max_fallbacks + 1]:
            client = await self._client(cand.provider, namespace_chain)
            options = {"effort": effort or cand.entry.meta.get("effort")}
            t0 = time.monotonic()
            try:
                result = await client.chat(model=cand.entry.model_name, system=system, messages=messages, tools=tools,
                                           max_tokens=max_tokens, options=options)
                return result, cand, int((time.monotonic() - t0) * 1000)
            except ProviderError as e:
                last = e
                if not e.retryable:
                    raise
                self._cooldown[cand.provider.name] = time.monotonic() + 60
                log.warning("model %s failed (%s); trying next candidate", cand.ref, e)
                await self.events.emit("model.fallback", f"{cand.ref} failed: {e}", level="warn", subject=cand.ref)
        raise ProviderError(f"all candidate models failed; last error: {last}", retryable=True)

    def cost(self, entry: ModelEntry, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * entry.price_in + output_tokens * entry.price_out) / 1_000_000

    async def health(self, provider_id: str) -> dict:
        async with self.db.session() as s:
            p = await s.get(ModelProvider, provider_id)
        if not p:
            raise NotFound("provider not found")
        return await (await self._client(p, ["ns_root"])).health()

    async def discover(self, provider_id: str) -> list[dict]:
        async with self.db.session() as s:
            p = await s.get(ModelProvider, provider_id)
        if not p:
            raise NotFound("provider not found")
        try:
            return await (await self._client(p, ["ns_root"])).discover()
        except Exception as e:
            raise AiosError(f"discovery failed: {type(e).__name__}: {e}") from e
