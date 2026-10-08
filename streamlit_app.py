import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

RAW_URL = ("https://raw.githubusercontent.com/chapsizator-bit/Variacotes/"
           "main/data/alerts.json")
LOCAL = Path(__file__).parent / "data" / "alerts.json"
TZ = "Europe/Paris"
REF_COLORS = ["#3b82f6", "#475569"]

st.set_page_config(page_title="Variacotes", page_icon="📉", layout="centered")


@st.cache_data(ttl=300)
def load_alerts():
    try:
        r = requests.get(RAW_URL, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception:
        if LOCAL.exists():
            return json.loads(LOCAL.read_text(encoding="utf-8"))
        return []


def short(b):
    return b.split(".")[0].capitalize()


def fmt_dt(s):
    return pd.to_datetime(s, utc=True).tz_convert(TZ).strftime("%d/%m %H:%M")


def to_df(series, end, current, hourly):
    df = pd.DataFrame(series)
    df["t"] = pd.to_datetime(df["t"], utc=True, format="ISO8601").dt.tz_convert(TZ)
    df = df.sort_values("t")
    if end > df["t"].iloc[-1]:
        df = pd.concat([df, pd.DataFrame({"t": [end], "p": [current]})],
                       ignore_index=True)
    if hourly:
        df = df.set_index("t")["p"].resample("1h").last().ffill().reset_index()
    return df


def get_outcomes(a):
    return a.get("outcomes") or [{
        "label": a["selection"], "selected": True, "open": a["open"],
        "current": a["current"], "series": a.get("series", [])}]


def make_chart(a, sel, refs, hourly):
    end = pd.to_datetime(a["updated_at"], utc=True).tz_convert(TZ)
    sdf = to_df(sel["series"], end, sel["current"], hourly)
    x0, x1 = sdf["t"].iloc[0], sdf["t"].iloc[-1]
    op, cur = sel["open"], sel["current"]
    hover = "%{x|%d/%m %H:%M}<br>Cote %{y:.2f}<extra>%{fullData.name}</extra>"

    fig = go.Figure()
    lows, highs = [op, cur, sdf["p"].min()], [op, cur, sdf["p"].max()]
    for i, r in enumerate(refs):
        if not r.get("series"):
            continue
        df = to_df(r["series"], end, r["current"], hourly)
        lows.append(df["p"].min())
        highs.append(df["p"].max())
        fig.add_trace(go.Scatter(
            x=df["t"], y=df["p"], mode="lines", name=short(r["bookmaker"]),
            line=dict(color=REF_COLORS[i % 2], width=2, shape="hv"),
            hovertemplate=hover))

    fig.add_trace(go.Scatter(
        x=[x0, x1], y=[op, op], mode="lines", showlegend=False,
        line=dict(color="#94a3b8", width=1.5, dash="dash"), hoverinfo="skip"))
    fig.add_trace(go.Scatter(
        x=sdf["t"], y=sdf["p"], mode="lines",
        name=f'{short(a["bookmaker"])} (sélection)',
        line=dict(color="#ef4444", width=3.5, shape="hv"),
        fill="tonexty", fillcolor="rgba(239,68,68,0.15)", hovertemplate=hover))
    fig.add_trace(go.Scatter(
        x=[x0, x1], y=[op, cur], mode="markers+text", showlegend=False,
        marker=dict(size=11, color=["#64748b", "#ef4444"],
                    line=dict(color="white", width=2)),
        text=f"Réf. {op:.2f}", f"{cur:.2f} ({cur - op:+.2f})"],
        textposition=["top right", "top left"], cliponaxis=False,
        hoverinfo="skip"))

    lo, hi = min(lows), max(highs)
    pad = max((hi - lo) * 0.3, 0.05)
    fig.update_layout(
        height=360, margin=dict(l=10, r=10, t=10, b=10), template="plotly_white",
        hovermode="x", yaxis=dict(range=[lo - pad, hi + pad], fixedrange=True),
        xaxis=dict(fixedrange=True),
        legend=dict(orientation="h", yanchor="top", y=-0.12, x=0))
    return fig


def show_chart(fig, key):
    cfg = {"displayModeBar": False, "scrollZoom": False}
    try:
        st.plotly_chart(fig, width="stretch", config=cfg, key=key)
    except TypeError:
        st.plotly_chart(fig, use_container_width=True, config=cfg, key=key)


def row(name, op, cur):
    d = cur - op
    arrow = "▼" if d < 0 else ("▲" if d > 0 else "=")
    return {"Nom": name, "Référence": f"{op:.2f}", "Actuelle": f"{cur:.2f}",
            "Variation": f"{arrow} {d:+.2f}"}


def books_table(a, refs):
    rows = [row("👉 " + short(a["bookmaker"]), a["open"], a["current"])]
    rows += [row(short(r["bookmaker"]), r["open"], r["current"]) for r in refs]
    return pd.DataFrame(rows).set_index("Nom")


def outcomes_table(outs):
    rows = [row(("👉 " if o.get("selected") else "") + o["label"], o["open"], o["current"])
            for o in outs]
    return pd.DataFrame(rows).set_index("Nom")


st.title("📉 Variacotes")

c_a, c_b = st.columns(2)
if c_a.button("🔄 Rafraîchir"):
    st.cache_data.clear()
    st.rerun()
hourly = c_b.toggle("Heure par heure", value=False)

alerts = load_alerts()
if not alerts:
    st.info("Aucune alerte pour le moment.")
    st.stop()

sport = st.selectbox("Sport", ["Tous"] + sorted({a["sport"] for a in alerts}))
upcoming = st.toggle("Matchs à venir uniquement", value=True)
now = pd.Timestamp.now(tz="UTC")

rows = [a for a in alerts
        if (sport == "Tous" or a["sport"] == sport)
        and (not upcoming or pd.to_datetime(a["start"], utc=True) > now)]
rows.sort(key=lambda a: a["alerted_at"], reverse=True)
st.caption(f"{len(rows)} alerte(s)")

for a in rows:
    outs = get_outcomes(a)
    sel = next((o for o in outs if o.get("selected")), outs[0])
    refs = a.get("refs") or []
    with st.container(border=True):
        st.subheader(a["match"])
        st.caption(f'{a["sport"]} · {a["league"]} · {short(a["bookmaker"])} · '
                   f'coup d\'envoi {fmt_dt(a["start"])}')
        st.markdown(f'**Sélection : {a["selection"]}**')
        c1, c2, c3 = st.columns(3)
        c1.metric("Référence", f'{a["open"]:.2f}')
        c2.metric("Actuelle", f'{a["current"]:.2f}')
        c3.metric("Variation", f'{-a["drop"]:+.2f}', f'{-a["pct"]:+.1f} %',
                  delta_color="off")
        if sel.get("series"):
            show_chart(make_chart(a, sel, refs, hourly), key=f'{a["id"]}|{hourly}')
        if refs:
            st.caption("Comparaison des bookmakers (même sélection)")
            st.table(books_table(a, refs))
        if len(outs) > 1:
            st.caption(f'Autres issues chez {short(a["bookmaker"])}')
            st.table(outcomes_table(outs))
        if a.get("flash"):
            st.link_button("🔎 Flashscore", a["flash"])
