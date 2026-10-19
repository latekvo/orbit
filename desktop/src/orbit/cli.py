"""``orbit`` command-line client for orbitd."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from .client import ApiError, LocalClient
from .daemon.timeconv import now_ms


def fmt_ms(ms: float) -> str:
    s = max(0, int(ms // 1000))
    h, rem = divmod(s, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def media_position(media: dict[str, Any], now: int) -> float:
    pos = float(media.get("positionMs") or 0)
    if media.get("state") == "playing" and isinstance(media.get("positionAtMs"), int):
        pos += (now - media["positionAtMs"]) * float(media.get("rate") or 1.0)
    dur = media.get("durationMs")
    return min(pos, dur) if isinstance(dur, int) and dur > 0 else pos


def chrono_text(chrono: dict[str, Any], now: int) -> str:
    if not chrono.get("running"):
        return f"{fmt_ms(chrono.get('valueMs') or 0)} (paused)"
    base = chrono.get("baseMs") or now
    return fmt_ms(base - now if chrono.get("countDown") else now - base)


def bar(value: float, maximum: float, width: int = 20) -> str:
    frac = 0.0 if maximum <= 0 else max(0.0, min(1.0, value / maximum))
    filled = round(frac * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def describe_activity(a: dict[str, Any], now: int, device_name: str) -> list[str]:
    head = f"{a.get('title') or a['id']}"
    if a.get("subtitle"):
        head += f" - {a['subtitle']}"
    app = (a.get("app") or {}).get("name") or (a.get("app") or {}).get("id") or "?"
    lines = [
        f"[{a.get('kind', 'generic')}] {head}",
        f"    id {a['id']}  app {app}  on {device_name} ({a['device'][:8]})",
    ]
    if a.get("text"):
        lines.append(f"    {a['text']}")
    media = a.get("media")
    if isinstance(media, dict):
        pos = media_position(media, now)
        dur = media.get("durationMs") or 0
        lines.append(f"    {media.get('state', '?')} {fmt_ms(pos)} / {fmt_ms(dur)} {bar(pos, dur)}")
    prog = a.get("progress")
    if isinstance(prog, dict):
        if prog.get("indeterminate"):
            lines.append("    progress: indeterminate")
        else:
            v, m = prog.get("value") or 0, prog.get("max") or 100
            lines.append(f"    progress {v}/{m} {bar(v, m)}")
    if isinstance(a.get("chrono"), dict):
        lines.append(f"    clock {chrono_text(a['chrono'], now)}")
    actions = a.get("actions") or []
    if actions:
        parts = []
        for act in actions:
            label = f"{act.get('id')} ({act.get('label', '')})"
            if act.get("input"):
                label += " [input]"
            parts.append(label)
        lines.append("    actions: " + ", ".join(parts))
    return lines


def device_line(d: dict[str, Any]) -> str:
    state = "connected via " + str(d.get("transport")) if d["connected"] else "offline"
    paired = "paired" if d["paired"] else "not paired"
    seen = ",".join(d.get("discovered") or []) or "-"
    who = f"{d['name'] or '?'} ({d.get('platform') or '?'})"
    return f"{d['id']}  {who}  {paired}, {state}  addr {d.get('address') or '-'}  seen {seen}"


def parse_value(text: str, force_string: bool) -> Any:
    if not force_string and re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


async def resolve_device(c: LocalClient, ref: str) -> str:
    devices = await c.call("devices.list")
    matches = [d for d in devices if d["id"] == ref] or [
        d for d in devices if d["id"].startswith(ref.lower()) or (d.get("name") or "").lower() == ref.lower()
    ]
    if len(matches) != 1:
        raise ApiError("not_found", f"{'no' if not matches else 'ambiguous'} device matching {ref!r}")
    return matches[0]["id"]


async def wait_for(c: LocalClient, event_name: str, predicate, timeout: float) -> Any:
    """Return the first event data satisfying ``predicate`` within ``timeout`` (requires subscribe)."""

    async def scan():
        async for event, data in c.events():
            if event == event_name and predicate(data):
                return data
        raise ConnectionError("orbitd closed the connection")

    return await asyncio.wait_for(scan(), timeout)


async def cmd_status(c: LocalClient, args: argparse.Namespace) -> None:
    st = await c.call("status")
    print(f"orbitd {st['version']} on {st['name']} (id {st['deviceId']})")
    devices = await c.call("devices.list")
    acts = await c.call("activities.list")
    connected = [d for d in devices if d["connected"]]
    print(f"{len(devices)} device(s), {len(connected)} connected, {len(acts)} activities")
    for d in devices:
        print("  " + device_line(d))


async def cmd_devices(c: LocalClient, args: argparse.Namespace) -> None:
    devices = await c.call("devices.list")
    if args.json:
        print(json.dumps(devices, indent=2))
    elif not devices:
        print("no devices (none paired or discovered)")
    for d in [] if args.json else devices:
        print(device_line(d))


async def cmd_connect(c: LocalClient, args: argparse.Namespace) -> None:
    target = args.target
    m = re.fullmatch(r"\[?([^\[\]]+?)\]?:(\d+)", target)
    if m and not re.fullmatch(r"[0-9a-f]{32}", target):
        params: dict[str, Any] = {"host": m.group(1), "port": int(m.group(2))}
    else:
        params = {"id": await resolve_device(c, target)}
    await c.call("subscribe")
    res = await c.call("devices.connect", params)
    dev_id = res["id"]
    devices = await c.call("devices.list")
    dev = next((d for d in devices if d["id"] == dev_id), None)
    print(f"connected to {dev['name'] if dev else '?'} ({dev_id})")
    pairings = [p for p in await c.call("pairing.list") if p["deviceId"] == dev_id]
    if pairings:
        p = pairings[0]
        print(f"pairing requested - code {p['sas']}; check it matches the phone, then run: orbit pair --accept")


def pairing_line(p: dict[str, Any]) -> str:
    state = []
    if p["localAccepted"]:
        state.append("accepted here")
    if p["remoteAccepted"]:
        state.append("accepted on phone")
    return f"{p['name']} ({p['deviceId']})  code {p['sas']}  session {p['session']}  {', '.join(state) or 'waiting'}"


async def cmd_pair(c: LocalClient, args: argparse.Namespace) -> None:
    await c.call("subscribe")
    pairings = await c.call("pairing.list")
    if not pairings and args.wait:
        try:
            pairings = (await wait_for(c, "pairing.changed", lambda d: d["pairings"], args.wait))["pairings"]
        except TimeoutError:
            pass
    if args.device:
        dev_id = await resolve_device(c, args.device)
        pairings = [p for p in pairings if p["deviceId"] == dev_id]
    if not pairings:
        print("no pending pairings")
        if args.accept or args.reject:
            sys.exit(1)
        return
    decided = []
    for p in pairings:
        print(pairing_line(p))
        if p["localAccepted"]:
            continue
        if args.accept or args.reject:
            accept = bool(args.accept)
        elif sys.stdin.isatty():
            answer = await asyncio.to_thread(input, f"Does the phone show {p['sas']}? Pair with {p['name']}? [y/N] ")
            accept = answer.strip().lower() in ("y", "yes")
        else:
            continue
        await c.call("pairing.respond", {"session": p["session"], "accept": accept})
        decided.append((p, accept))
    for p, accept in decided:
        if not accept:
            print(f"rejected {p['name']}")
            continue
        print(f"accepted {p['name']} - waiting for the phone to confirm...")
        try:
            paired = await asyncio.wait_for(pairing_outcome(c, p), args.timeout)
        except TimeoutError:
            print(f"phone did not confirm within {args.timeout:g} s (still pending; see `orbit pair`)")
            sys.exit(1)
        if not paired:
            print(f"pairing with {p['name']} failed (rejected on the phone or connection lost)")
            sys.exit(1)
        print(f"paired with {p['name']} ({p['deviceId']})")


async def pairing_outcome(c: LocalClient, p: dict[str, Any]) -> bool:
    """Wait until the pairing session is no longer pending; True if the device ended up paired and connected."""

    def pending(pairings: list[dict[str, Any]]) -> bool:
        return any(x["session"] == p["session"] for x in pairings)

    if pending(await c.call("pairing.list")):
        async for event, data in c.events():
            if event == "pairing.changed" and not pending(data["pairings"]):
                break
    device = next((d for d in await c.call("devices.list") if d["id"] == p["deviceId"]), None)
    return bool(device and device["paired"] and device["connected"])


async def cmd_activities(c: LocalClient, args: argparse.Namespace) -> None:
    acts = await c.call("activities.list")
    if args.json:
        print(json.dumps(acts, indent=2))
        return
    names = {d["id"]: d["name"] for d in await c.call("devices.list")}
    if not acts:
        print("no activities")
    now = now_ms()
    for a in acts:
        print("\n".join(describe_activity(a, now, names.get(a["device"], "?"))))


async def cmd_action(c: LocalClient, args: argparse.Namespace) -> None:
    params: dict[str, Any] = {
        "device": await resolve_device(c, args.device),
        "activity": args.activity,
        "action": args.action,
    }
    if args.value is not None:
        params["value"] = parse_value(args.value, args.string)
    res = await c.call("activities.action", params)
    if res.get("ok"):
        print("ok")
    else:
        print(f"phone refused: {res.get('error') or 'no reason given'}", file=sys.stderr)
        sys.exit(1)


async def cmd_disconnect(c: LocalClient, args: argparse.Namespace) -> None:
    await c.call("devices.disconnect", {"id": await resolve_device(c, args.device)})
    print("disconnected")


async def cmd_forget(c: LocalClient, args: argparse.Namespace) -> None:
    dev_id = await resolve_device(c, args.device)
    await c.call("devices.forget", {"id": dev_id})
    print(f"forgot {dev_id}")


async def cmd_watch(c: LocalClient, args: argparse.Namespace) -> None:
    await c.call("subscribe")
    names: dict[str, str] = {}
    async for event, data in c.events():
        if args.json:
            print(json.dumps({"event": event, "data": data}), flush=True)
            continue
        if event == "devices.changed":
            names = {d["id"]: d["name"] for d in data["devices"]}
            print(f"-- devices ({len(data['devices'])})")
            for d in data["devices"]:
                print("   " + device_line(d))
        elif event == "pairing.changed":
            print(f"-- pairings ({len(data['pairings'])})")
            for p in data["pairings"]:
                print("   " + pairing_line(p))
        elif event == "activities.changed":
            print(f"-- activities ({len(data['activities'])})")
            now = now_ms()
            for a in data["activities"]:
                print("\n".join("   " + line for line in describe_activity(a, now, names.get(a["device"], "?"))))
        sys.stdout.flush()


COMMANDS = {
    "status": cmd_status,
    "devices": cmd_devices,
    "connect": cmd_connect,
    "pair": cmd_pair,
    "activities": cmd_activities,
    "action": cmd_action,
    "disconnect": cmd_disconnect,
    "forget": cmd_forget,
    "watch": cmd_watch,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orbit", description="Control orbitd, the Orbit desktop daemon.")
    p.add_argument("--socket", help="orbitd socket (default: $ORBIT_SOCKET or platform default)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="daemon and device overview")
    s = sub.add_parser("devices", help="list paired and discovered devices")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("connect", help="connect to a device id/name or host:port (pairs if needed)")
    s.add_argument("target", help="device id (or unique prefix / name) or host:port")
    s = sub.add_parser("pair", help="show pending pairings and accept/reject them")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--accept", action="store_true", help="accept all pending pairings without asking")
    g.add_argument("--reject", action="store_true", help="reject all pending pairings")
    s.add_argument("--device", help="only pairings with this device")
    s.add_argument("--wait", type=float, default=0, help="wait up to N seconds for a pairing to appear")
    s.add_argument("--timeout", type=float, default=60, help="seconds to wait for the phone to confirm")
    s = sub.add_parser("activities", help="list live activities")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("action", help="trigger an activity action")
    s.add_argument("device")
    s.add_argument("activity")
    s.add_argument("action")
    s.add_argument("value", nargs="?", help="optional value (integers are sent as numbers unless --string)")
    s.add_argument("--string", action="store_true", help="always send the value as a string")
    s = sub.add_parser("disconnect", help="disconnect (until `orbit connect`)")
    s.add_argument("device")
    s = sub.add_parser("forget", help="unpair and remove all data of a device")
    s.add_argument("device")
    s = sub.add_parser("watch", help="stream events")
    s.add_argument("--json", action="store_true", help="raw event JSON lines")
    return p


async def run(args: argparse.Namespace) -> None:
    try:
        c = await LocalClient.connect(Path(args.socket) if args.socket else None)
    except OSError as e:
        print(f"orbit: cannot reach orbitd ({e}); is it running?", file=sys.stderr)
        sys.exit(2)
    try:
        await COMMANDS[args.command](c, args)
    finally:
        await c.close()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(run(args))
    except ApiError as e:
        print(f"orbit: {e.code}: {e.message}", file=sys.stderr)
        sys.exit(1)
    except ConnectionError as e:
        print(f"orbit: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
