"""Render sample bot embeds to an HTML page that looks like Discord.

UI work is impossible to review from source alone — an embed is a pile of
strings until Discord lays it out. This renders any ``discord.Embed`` (plus
plain-text replies) into a static page styled like the real client, so a
change to ``app/ui.py`` can be *looked at* instead of imagined.

    python scripts/preview_ui.py            # writes previews/ui.html
    python scripts/preview_ui.py --open     # ...and opens it

Deliberately imports only ``app.ui`` (pure, Discord-free apart from
``discord.Embed`` itself) so it runs without a token, a database, or config.
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from app import ui  # noqa: E402

OUT = ROOT / "previews" / "ui.html"

# --------------------------------------------------------------------------
# Minimal Discord-flavoured markdown -> HTML. Only the subset the bot emits.
# --------------------------------------------------------------------------

_SUBTEXT = re.compile(r"^-# (.*)$", re.MULTILINE)
_H3 = re.compile(r"^### (.*)$", re.MULTILINE)
_H2 = re.compile(r"^## (.*)$", re.MULTILINE)
_H1 = re.compile(r"^# (.*)$", re.MULTILINE)
_CODEBLOCK = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_UNDERSCORE_ITALIC = re.compile(r"(?<!_)_([^_\n]+)_(?!_)")
_QUOTE = re.compile(r"^&gt; (.*)$", re.MULTILINE)
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def md(text: str) -> str:
    """Render the Discord-markdown subset the bot uses."""
    if not text:
        return ""
    out = html.escape(text)
    blocks: list[str] = []

    def _stash(m: re.Match) -> str:
        blocks.append(m.group(1))
        return f"\x00{len(blocks) - 1}\x00"

    out = _CODEBLOCK.sub(_stash, out)
    out = _LINK.sub(r'<a href="\2">\1</a>', out)
    out = _SUBTEXT.sub(r'<span class="subtext">\1</span>', out)
    out = _H3.sub(r'<div class="h3">\1</div>', out)
    out = _H2.sub(r'<div class="h2">\1</div>', out)
    out = _H1.sub(r'<div class="h1">\1</div>', out)
    out = _QUOTE.sub(r'<span class="quote">\1</span>', out)
    out = _BOLD.sub(r"<b>\1</b>", out)
    out = _ITALIC.sub(r"<i>\1</i>", out)
    out = _UNDERSCORE_ITALIC.sub(r"<i>\1</i>", out)
    out = _CODE.sub(r'<code class="inline">\1</code>', out)
    out = out.replace("\n", "<br>")
    for i, body in enumerate(blocks):
        out = out.replace(f"\x00{i}\x00", f"<pre>{body.rstrip()}</pre>")
    return out


def render_embed(e: discord.Embed) -> str:
    colour = f"#{e.colour.value:06x}" if e.colour else "#4f545c"
    parts = [f'<div class="embed" style="border-color:{colour}">']
    parts.append('<div class="embed-inner">')
    if e.author and e.author.name:
        icon = (
            f'<img class="author-icon" src="{html.escape(e.author.icon_url)}">'
            if e.author.icon_url else ""
        )
        parts.append(
            f'<div class="author">{icon}'
            f"<span>{html.escape(e.author.name)}</span></div>"
        )
    if e.title:
        parts.append(f'<div class="title">{md(e.title)}</div>')
    if e.description:
        parts.append(f'<div class="desc">{md(e.description)}</div>')
    if e.fields:
        parts.append('<div class="fields">')
        run: list[discord.embeds.EmbedProxy] = []

        def _flush() -> None:
            if not run:
                return
            parts.append('<div class="inline-row">')
            for f in run:
                parts.append(
                    f'<div class="field inline" style="flex-basis:'
                    f'{100 / len(run):.4f}%">'
                    f'<div class="fname">{md(f.name)}</div>'
                    f'<div class="fval">{md(f.value)}</div></div>'
                )
            parts.append("</div>")
            run.clear()

        for f in e.fields:
            if f.inline:
                run.append(f)
                if len(run) == 3:
                    _flush()
            else:
                _flush()
                parts.append(
                    f'<div class="field">'
                    f'<div class="fname">{md(f.name)}</div>'
                    f'<div class="fval">{md(f.value)}</div></div>'
                )
        _flush()
        parts.append("</div>")
    if e.image and e.image.url:
        parts.append(f'<img class="embed-image" src="{html.escape(e.image.url)}">')
    parts.append("</div>")
    if e.thumbnail and e.thumbnail.url:
        parts.append(
            f'<img class="thumb" src="{html.escape(e.thumbnail.url)}">'
        )
    parts.append("</div>")
    foot = []
    if e.footer and e.footer.text:
        foot.append(html.escape(e.footer.text))
    if e.timestamp:
        foot.append(e.timestamp.strftime("%d/%m/%Y %H:%M"))
    if foot:
        parts.append(f'<div class="footer">{" • ".join(foot)}</div>')
    return "".join(parts)


def _len_report(e: discord.Embed) -> str:
    """Discord rejects oversize embeds at send time — surface the budget."""
    total = len(e)
    warn = []
    if total > 6000:
        warn.append(f"TOTAL {total}>6000")
    if e.title and len(e.title) > 256:
        warn.append(f"title {len(e.title)}>256")
    if e.description and len(e.description) > 4096:
        warn.append(f"desc {len(e.description)}>4096")
    if len(e.fields) > 25:
        warn.append(f"fields {len(e.fields)}>25")
    for f in e.fields:
        if len(f.value) > 1024:
            warn.append(f"field {f.name!r} {len(f.value)}>1024")
    tag = "over" if warn else "ok"
    detail = "; ".join(warn) if warn else f"{total}/6000 chars"
    return f'<span class="budget {tag}">{detail}</span>'


CSS = """
:root{--bg:#313338;--card:#2b2d31;--txt:#dbdee1;--mut:#949ba4;--acc:#f26522;
--code:#1e1f22;--line:#3f4147}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
font-family:"gg sans","Noto Sans",Helvetica,Arial,sans-serif;font-size:16px;
line-height:1.375}
.wrap{max-width:900px;margin:0 auto;padding:32px 16px 96px}
h1.page{font-size:22px;margin:0 0 4px;color:#fff}
p.lede{color:var(--mut);margin:0 0 32px;font-size:14px}
.case{margin:0 0 40px;border-top:1px solid var(--line);padding-top:20px}
.case > h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;
color:var(--mut);margin:0 0 12px;font-weight:700}
.msg{display:flex;gap:16px;padding:6px 0}
.avatar{width:40px;height:40px;border-radius:50%;flex:0 0 40px;
background:var(--acc);display:flex;align-items:center;justify-content:center;
font-size:18px}
.body{min-width:0;flex:1}
.head{display:flex;align-items:baseline;gap:8px;margin-bottom:2px}
.name{color:#fff;font-weight:500;font-size:15px}
.bot{background:#5865f2;color:#fff;font-size:10px;font-weight:600;
padding:1px 4px;border-radius:3px;text-transform:uppercase;
position:relative;top:-1px}
.time{color:var(--mut);font-size:12px}
.embed{border-left:4px solid var(--acc);background:var(--card);
border-radius:4px;max-width:520px;padding:8px 16px 16px 12px;
display:grid;grid-template-columns:1fr auto;gap:16px;margin-top:2px}
.embed-inner{min-width:0}
.author{display:flex;align-items:center;gap:8px;margin-top:8px;
font-size:14px;font-weight:600;color:#fff}
.author-icon{width:24px;height:24px;border-radius:50%}
.title{margin-top:8px;font-size:16px;font-weight:700;color:#fff}
.desc{margin-top:8px;font-size:14px;color:var(--txt);white-space:normal}
.fields{margin-top:8px;display:flex;flex-direction:column;gap:8px}
.inline-row{display:flex;gap:8px}
.field.inline{min-width:0}
.fname{font-size:14px;font-weight:700;color:#fff;margin-bottom:2px}
.fval{font-size:14px;color:var(--txt)}
.thumb{width:80px;height:80px;border-radius:50%;object-fit:cover;
align-self:start;margin-top:8px}
.embed-image{margin-top:16px;max-width:100%;border-radius:4px;display:block}
.footer{max-width:520px;background:var(--card);border-left:4px solid var(--acc);
margin-top:-16px;padding:0 12px 12px;border-radius:0 0 4px 4px;
font-size:12px;color:var(--mut)}
code.inline{background:var(--code);padding:.1em .3em;border-radius:3px;
font-family:Consolas,"Courier New",monospace;font-size:.85em;
white-space:pre}
pre{background:var(--code);border:1px solid #2b2d31;border-radius:4px;
padding:8px;font-family:Consolas,monospace;font-size:.85em;overflow-x:auto;
margin:6px 0;white-space:pre}
.subtext{color:var(--mut);font-size:.8em}
.quote{border-left:4px solid var(--line);padding-left:8px;display:inline-block}
.h1{font-size:1.5em;font-weight:700;color:#fff;margin:8px 0 2px}
.h2{font-size:1.25em;font-weight:700;color:#fff;margin:8px 0 2px}
.h3{font-size:1em;font-weight:700;color:#fff;margin:8px 0 2px}
.plain{font-size:15px;white-space:normal}
.budget{font-size:11px;color:var(--mut);font-family:Consolas,monospace;
display:inline-block;margin-top:6px}
.budget.over{color:#ed4245;font-weight:700}
.buttons{display:flex;gap:8px;margin-top:8px;max-width:520px}
.btn{background:#4e5058;color:#fff;font-size:14px;font-weight:500;
padding:8px 16px;border-radius:3px}
.btn.primary{background:#5865f2}
.btn.success{background:#248046}
"""


def _msg(inner: str, *, who: str = "gym-bot", icon: str = "🏋️") -> str:
    return (
        f'<div class="msg"><div class="avatar">{icon}</div>'
        f'<div class="body"><div class="head"><span class="name">{who}</span>'
        f'<span class="bot">bot</span>'
        f'<span class="time">Today at 19:42</span></div>{inner}</div></div>'
    )


def build_page(cases: list[tuple[str, list]]) -> str:
    out = [
        "<!doctype html><meta charset='utf-8'>",
        "<title>gym-bot UI preview</title>",
        f"<style>{CSS}</style>",
        '<div class="wrap">',
        '<h1 class="page">gym-bot — Discord UI preview</h1>',
        '<p class="lede">Rendered from the real helpers in '
        "<code class='inline'>app/ui.py</code>. Character budgets are "
        "checked against Discord's limits.</p>",
    ]
    for title, items in cases:
        out.append(f'<div class="case"><h2>{html.escape(title)}</h2>')
        for item in items:
            if isinstance(item, discord.Embed):
                out.append(_msg(render_embed(item) + _len_report(item)))
            elif isinstance(item, str):
                out.append(_msg(f'<div class="plain">{md(item)}</div>'))
            elif isinstance(item, tuple):  # (embed, [button labels])
                embed, btns = item
                bar = "".join(
                    f'<div class="btn {c}">{html.escape(t)}</div>'
                    for t, c in btns
                )
                body = render_embed(embed) if embed is not None else ""
                out.append(
                    _msg(body + f'<div class="buttons">{bar}</div>')
                )
        out.append("</div>")
    out.append("</div>")
    return "".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--open", action="store_true", help="open in a browser")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    cases = ui.preview_cases()
    page = build_page(cases)
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(page, encoding="utf-8")
    n = sum(len(items) for _t, items in cases)
    print(f"wrote {dest} ({n} samples across {len(cases)} cases)")
    if args.open:
        webbrowser.open(dest.resolve().as_uri())


if __name__ == "__main__":
    main()
