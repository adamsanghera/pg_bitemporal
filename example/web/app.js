/* The Late Letter: a game about application time and transaction time.
 *
 * The server owns the truth: every row on the chart and every answer a goal
 * checks comes from the bitemporal tables in Postgres. This file only draws
 * them and walks the player through one idea at a time.
 */
(function () {
  "use strict";

  const DAY = 864e5;
  let W = 900;
  let ML = 120;
  const MR = 28;
  const MT = 30;
  const MB = 62;
  const STRIP_H = 64;
  let PLANE_H = 400;
  const CURRENT = "residence";
  const HISTORY = "residence_history";
  const STORE = "late-letter-progress";

  const $ = (id) => document.getElementById(id);
  const fmt = d3.utcFormat("%-d %b %Y");
  const fmtShort = d3.utcFormat("%-d %b");
  const fmtTick = (d) =>
    d.getUTCDate() !== 1
      ? d3.utcFormat("%-d %b")(d)
      : d.getUTCMonth() === 0
        ? d3.utcFormat("%b %Y")(d)
        : d3.utcFormat("%b")(d);
  const day = (s) =>
    s == null ? null : Date.parse(s.length === 10 ? s + "T00:00:00Z" : s);
  const isoDay = (ms) => new Date(ms).toISOString().slice(0, 10);
  const snap = (ms) => Math.round(ms / DAY) * DAY;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const esc = (s) =>
    String(s).replace(
      /[&<>"']/g,
      (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
          c
        ],
    );
  const appDate = (ms) => `<span class="app-ink">${fmt(ms)}</span>`;
  const txnDate = (ms) => `<span class="txn-ink">${fmt(ms)}</span>`;

  // ------------------------------------------------------------ chapters

  const CHAPTERS = [
    {
      id: "one",
      title: "Where did Ada live?",
      teaser: "One customer, one timeline.",
      person: "Ada",
      today: "2022-07-01",
      dims: 1,
      start: { app: "2022-03-01" },
      intro: [
        "Welcome to the records office. This strip is the bank's address book for one customer, Ada.",
        "Left to right is Ada's life. Each colored block is an address the bank has on file for her.",
      ],
      goals: [
        {
          kind: "pin",
          text: "Drag the pin to a day when Ada lived on Banana St.",
          hint: "Click or drag anywhere on the strip. Arrow keys nudge the pin one day at a time.",
          test: (s) => s.street === "456 Banana St",
          why: "The pin reads the strip like a calendar.",
        },
        {
          kind: "quiz",
          text: "Where did Ada live on 1 March 2022?",
          choices: ["123 Apple St", "456 Banana St", "No address on file"],
          answer: "123 Apple St",
          wrong: "Move the pin to 1 March and read the sentence under the strip.",
          why: "The Apple St block covers 1 March.",
        },
      ],
      outro:
        "That axis has a name: <b class='app-ink'>application time</b> (some people call it valid time). It's when something was true in the world. Most databases stop there. A bank can't, and the next chapter shows why.",
    },
    {
      id: "two",
      title: "The late letter",
      teaser: "When did the bank find out?",
      person: "Ada",
      today: "2022-07-01",
      dims: 1,
      start: { app: "2022-06-16" },
      intro: [
        "Here's the catch. Ada moved on 13 June, but her letter didn't reach the bank until 20 June.",
        "For a week the bank had her address wrong and had no way to know. To keep a record of that week, the address book needs a second axis.",
      ],
      goals: [
        {
          kind: "action",
          text: "Add the bank's own calendar to the chart.",
          button: "Unfold the second axis",
          run: () => setDims(2),
          why: "Going up is the bank's own calendar: <span class='txn-ink'>when it believed</span> each block. The dotted lines mark today on both axes.",
        },
        {
          kind: "pin",
          text: "On 16 June the bank mailed Ada a statement. Put the dot on 16 June in Ada's life, <i>as the bank saw things on 16 June</i>.",
          hint: "Drag the dot. The <span class='app-ink'>red line</span> is Ada's life; the <span class='txn-ink'>green line</span> is what the bank knew. Arrow keys nudge by a day.",
          test: (s) =>
            Math.abs(s.app - day("2022-06-16")) <= DAY &&
            Math.abs(s.txn - day("2022-06-16")) <= DAY,
          why: "Both lines on 16 June.",
        },
        {
          kind: "quiz",
          text: "Where did the bank mail the statement?",
          choices: ["123 Apple St", "456 Banana St"],
          answer: "123 Apple St",
          wrong: "Read the sentence under the chart.",
          why: "On 16 June the bank hadn't opened Ada's letter yet, so it still believed Apple St.",
        },
        {
          kind: "quiz",
          text: "Slide the green line up to today. Where did Ada <i>really</i> live on 16 June?",
          choices: ["123 Apple St", "456 Banana St"],
          answer: "456 Banana St",
          wrong: "Keep the red line on 16 June and drag the green line up past 20 June.",
          why: "Today the bank knows better. It didn't erase what it used to believe, though: the Apple St block under 20 June is still there.",
        },
      ],
      outro:
        "Two questions, two axes. <b class='app-ink'>Application time</b>: when was it true? <b class='txn-ink'>Transaction time</b>: when did we believe it? A table that keeps both is <b>bitemporal</b>, and that's what pg_bitemporal does in Postgres.",
    },
    {
      id: "three",
      title: "Open the mail",
      teaser: "Make your first write.",
      person: "Bo",
      today: "2022-03-10",
      dims: 2,
      tools: ["record"],
      start: { app: "2022-03-05" },
      preset: { street: "9 Elm Ave", written: "2022-03-10" },
      intro: [
        "Your first real job. Today is 10 March 2022, and there's a letter from Bo on your desk:",
        "<blockquote>I moved to 9 Elm Ave on 1 March. — Bo</blockquote>",
        "Write it down using the form under the chart. Keep an eye on the blocks as you fill it in.",
      ],
      goals: [
        {
          kind: "db",
          text: "Record that Bo has lived at 9 Elm Ave since 1 March, written down today.",
          hint: "Set “from” to 1 March and leave “still there” ticked. The write date is today, 10 March.",
          probes: [
            ["2022-03-01", "2022-03-11", "9 Elm Ave"],
            ["2022-02-28", "2022-03-11", "12 Oak Rd"],
            ["2022-03-05", "2022-03-09", "12 Oak Rd"],
          ],
          why: "The dashed outline showed where your write would land before you sent it.",
        },
        {
          kind: "quiz",
          text: "As of 9 March, where did the bank believe Bo was living on 5 March?",
          choices: ["12 Oak Rd", "9 Elm Ave"],
          answer: "12 Oak Rd",
          wrong: "Put the green line on 9 March and the red line on 5 March.",
          why: "Your write happened on 10 March. Everything the bank believed before that day is left as it was.",
        },
      ],
      outro:
        "Nothing was overwritten. The Oak Rd block now stops at 10 March, and your belief starts there. In SQL that took one UPDATE and one INSERT, and a trigger copied the old version into a history table. Next chapter you get to look inside.",
    },
    {
      id: "four",
      title: "A clerk's mistake",
      teaser: "Fix an error without hiding it.",
      person: "Cy",
      today: "2022-04-15",
      dims: 2,
      tools: ["record"],
      hood: true,
      start: { app: "2022-04-10" },
      preset: { written: "2022-04-15" },
      intro: [
        "On 3 April a clerk recorded that Cy moved to 88 Maple Dr. Wrong customer: this Cy never moved.",
        "Today is 15 April. Correct the book so that the bank now believes Cy has lived at 3 Birch Ln the whole time.",
      ],
      goals: [
        {
          kind: "db",
          text: "Correct it: from today on, the bank should believe Cy never left 3 Birch Ln.",
          hint: "Record 3 Birch Ln from 1 April, still there, written down on 15 April.",
          probes: [
            ["2022-04-10", "2022-04-16", "3 Birch Ln"],
            ["2022-02-01", "2022-04-16", "3 Birch Ln"],
            ["2022-04-10", "2022-04-05", "88 Maple Dr"],
          ],
          why: "The mistake isn't on the current record any more.",
        },
        {
          kind: "quiz",
          hood: true,
          text: "Open <b>Under the hood</b> (below the chart) and find the 88 Maple Dr row. Which table is it in now?",
          choices: [CURRENT, HISTORY, "Deleted for good"],
          answer: HISTORY,
          wrong: "Look at both tables in Under the hood.",
          why: "Whenever a row in <code>residence</code> is updated or deleted, the <code>record_txn_history</code> trigger copies the old version into <code>residence_history</code> and closes its <code>txn_period</code> at the moment of the write.",
        },
        {
          kind: "quiz",
          text: "As of 5 April, what did the bank believe about where Cy lived on 10 April?",
          choices: ["3 Birch Ln", "88 Maple Dr"],
          answer: "88 Maple Dr",
          wrong: "Move the green line down to 5 April.",
          why: "That's the point. An auditor can still see that the bank believed the mistake from 3 April to 15 April.",
        },
      ],
      outro:
        "You fixed the present without rewriting the past. In bitemporal terms that's a <b>correction</b>: a new belief about an old period.",
    },
    {
      id: "five",
      title: "No backdating",
      teaser: "The one rule the ledger enforces.",
      person: "Dee",
      today: "2022-05-20",
      dims: 2,
      tools: ["record"],
      hood: true,
      start: { app: "2022-04-15" },
      preset: {
        street: "40 Walnut Way",
        from: "2022-04-01",
        written: "2022-04-01",
      },
      intro: [
        "Dee moved to 40 Walnut Way on 1 May. The bank wrote it down on 2 May.",
        "Your manager leans in: “Her lease actually started 1 April. Put that in, and date it 1 April so it looks like we always knew.”",
      ],
      goals: [
        {
          kind: "error",
          code: "backdated",
          text: "Try it your manager's way: 40 Walnut Way from 1 April, written down on 1 April.",
          hint: "The form is already filled in. Press the button.",
          why: "Postgres refused. The Walnut Way row was written on 2 May, and nothing can be written before that. Transaction time only moves forward.",
        },
        {
          kind: "db",
          text: "Now do it honestly: the same fact, written down today.",
          hint: "Change “written down on” to 20 May.",
          probes: [
            ["2022-04-15", "2022-05-21", "40 Walnut Way"],
            ["2022-04-15", "2022-05-19", "5 Cedar Ct"],
          ],
          why: "Same fact, honest date.",
        },
        {
          kind: "quiz",
          text: "As of 10 May, where did the bank believe Dee lived on 15 April?",
          choices: ["5 Cedar Ct", "40 Walnut Way"],
          answer: "5 Cedar Ct",
          wrong: "Put the red line on 15 April and the green line on 10 May.",
          why: "You changed what the bank believes about April. What it believed back in April stays the same.",
        },
      ],
      outro:
        "That's the whole idea. You can revise what you believe about the past, but you can't change what you believed back then. That's why an auditor can trust the book.",
    },
    {
      id: "six",
      title: "Free play",
      teaser: "Every tool, every table.",
      person: "Ada",
      dims: 2,
      tools: ["record", "forget"],
      hood: true,
      sandbox: true,
      start: { app: "2022-06-14" },
      intro: [
        "Everything's unlocked. Pick any customer or type a new name, then record, forget, and watch the tables under the hood.",
        "Some things to try:<ul><li>Forget a few weeks in the middle of an address. <code>split_record</code> cuts the block in two.</li><li>Write twice on the same day. Only the last write of a day is kept.</li><li>Record an address with an end date in the past.</li></ul>",
      ],
      goals: [],
    },
  ];

  // ------------------------------------------------------------ state

  const S = {
    ch: null,
    index: 0,
    person: "Ada",
    rows: [],
    people: [],
    app: 0,
    txn: 0,
    dims: 1,
    goal: 0,
    done: [],
    feedback: "",
    showHint: false,
    mode: "record",
    ghost: null,
    lastWrite: null,
    lastAsk: null,
    hover: null,
    hoodOpen: false,
    hoodTab: "tables",
    busy: false,
  };
  const progress = loadProgress();
  const colors = new Map();
  const PALETTE = [
    "#c5d4b0",
    "#efd089",
    "#e3a397",
    "#9eb8d4",
    "#d4b8d8",
    "#b5d6cf",
    "#e8c3a0",
  ];

  function loadProgress() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORE) || "{}");
      return { done: saved.done || {}, seenHood: !!saved.seenHood };
    } catch {
      return { done: {}, seenHood: false };
    }
  }

  function saveProgress() {
    try {
      localStorage.setItem(STORE, JSON.stringify(progress));
    } catch {
      /* private window: progress lives for this visit only */
    }
  }

  function colorOf(street) {
    if (!colors.has(street)) {
      colors.set(street, PALETTE[colors.size % PALETTE.length]);
    }
    return colors.get(street);
  }

  // ------------------------------------------------------------ data

  async function api(path, body) {
    const init =
      body === undefined
        ? {}
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          };
    const response = await fetch(path, init);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok && payload.ok === undefined) {
      throw new Error(payload.error || response.statusText);
    }
    return payload;
  }

  function keyOf(r) {
    return r.table === CURRENT ? `c${r.row_id}` : `h${r.row_id}:${r.txn[0]}`;
  }

  function norm(r) {
    return {
      ...r,
      key: keyOf(r),
      current: r.table === CURRENT,
      a0: r.app[0] ? day(r.app[0]) : -Infinity,
      a1: r.app[1] ? day(r.app[1]) : Infinity,
      t0: r.txn[0] ? day(r.txn[0]) : -Infinity,
      t1: r.txn[1] ? day(r.txn[1]) : Infinity,
    };
  }

  function setRows(rows) {
    S.rows = rows.map(norm);
    S.rows.forEach((r) => colorOf(r.street));
  }

  function covers(r, app, txn) {
    return r.a0 <= app && app < r.a1 && r.t0 <= txn && txn < r.t1;
  }

  function believed(app, txn) {
    const hits = S.rows.filter((r) => covers(r, app, txn));
    return { row: hits.length === 1 ? hits[0] : null, count: hits.length };
  }

  function todayMs() {
    const latest = d3.max(S.rows, (r) => (isFinite(r.t0) ? r.t0 : null));
    const base = S.ch && S.ch.today ? day(S.ch.today) : null;
    const known = [latest, base].filter((v) => v != null);
    return known.length ? Math.max(...known) : day("2022-07-01");
  }

  function domains() {
    const today = todayMs();
    const apps = [];
    const txns = [today];
    S.rows.forEach((r) => {
      [r.a0, r.a1].forEach((v) => isFinite(v) && apps.push(v));
      [r.t0, r.t1].forEach((v) => isFinite(v) && txns.push(v));
    });
    if (S.ghost) {
      [S.ghost.a0, S.ghost.a1].forEach((v) => isFinite(v) && apps.push(v));
      if (isFinite(S.ghost.t0)) txns.push(S.ghost.t0);
    }
    if (!apps.length) apps.push(today - 90 * DAY, today);
    const aLo = d3.min(apps) - 14 * DAY;
    const aHi = Math.max(d3.max(apps), today) + 45 * DAY;
    // Keep the recent past readable: older beliefs run off the bottom edge.
    const tLo = Math.max(d3.min(txns) - 10 * DAY, today - 110 * DAY);
    const tTop = Math.max(d3.max(txns), today);
    const tHi = tTop + Math.max(18 * DAY, (tTop - tLo) * 0.2);
    return { aLo, aHi, tLo, tHi, today };
  }

  // ------------------------------------------------------------ chart

  const svg = d3.select("#chart");
  const G = {};
  let X = d3.scaleUtc();
  let Y = d3.scaleUtc();
  let D = domains();
  let dragging = false;

  function plotH() {
    return S.dims === 2 ? PLANE_H : STRIP_H;
  }

  function setupChart() {
    svg
      .attr("viewBox", `0 0 ${W} ${MT + STRIP_H + MB}`)
      .attr("tabindex", 0)
      .attr("role", "application")
      .attr(
        "aria-label",
        "The address book. Arrow keys move the pin; hold Shift to move a week.",
      );
    const defs = svg.append("defs");
    const hatch = defs
      .append("pattern")
      .attr("id", "hatch")
      .attr("patternUnits", "userSpaceOnUse")
      .attr("width", 7)
      .attr("height", 7)
      .attr("patternTransform", "rotate(45)");
    hatch.append("line").attr("class", "hatch-line").attr("y2", 7);
    G.clip = defs.append("clipPath").attr("id", "plot-clip").append("rect");

    G.bg = svg.append("rect").attr("class", "plot-bg");
    G.future = svg.append("rect").attr("class", "future");
    G.rects = svg.append("g").attr("clip-path", "url(#plot-clip)");
    G.ghost = svg
      .append("rect")
      .attr("class", "ghost")
      .attr("clip-path", "url(#plot-clip)")
      .attr("opacity", 0);
    G.today = svg.append("g").attr("class", "today").attr("opacity", 0);
    G.today.append("line");
    G.today.append("text").attr("text-anchor", "end");
    // The same day on the other axis: where "now" falls in the customer's life.
    G.todayV = svg.append("g").attr("class", "today").attr("opacity", 0);
    G.todayV.append("line");
    G.todayV.append("text").attr("text-anchor", "start");
    G.futureLabel = svg
      .append("text")
      .attr("class", "future-label")
      .attr("opacity", 0)
      .text("↑ still believed, until someone writes again");
    G.ax = svg.append("g").attr("class", "axis");
    G.ay = svg.append("g").attr("class", "axis").attr("opacity", 0);
    G.tx = svg
      .append("text")
      .attr("class", "axis-title app-fill")
      .attr("text-anchor", "middle");
    G.ty = svg
      .append("text")
      .attr("class", "axis-title txn-fill")
      .attr("text-anchor", "middle")
      .attr("opacity", 0);

    G.cross = svg.append("g").attr("class", "cross");
    G.vline = G.cross.append("line").attr("class", "app-stroke");
    G.hline = G.cross
      .append("line")
      .attr("class", "txn-stroke")
      .attr("opacity", 0);
    G.dot = G.cross.append("circle").attr("class", "dot").attr("r", 7);
    G.appChip = chip("app");
    G.txnChip = chip("txn");
    G.txnChip.attr("opacity", 0);

    svg.on("pointerdown", (event) => {
      // Stop the browser from starting a text selection or a native drag;
      // that also skips the default focus, so take focus for the arrow keys.
      event.preventDefault();
      svg.node().focus({ preventScroll: true });
      const [px, py] = d3.pointer(event);
      if (!inPlot(px, py)) return;
      dragging = true;
      document.documentElement.classList.add("dragging");
      svg.node().setPointerCapture(event.pointerId);
      hideTip();
      moveTo(px, py);
    });
    svg.on("pointermove", (event) => {
      const [px, py] = d3.pointer(event);
      if (dragging) moveTo(px, py);
      else hoverAt(px, py, event);
    });
    svg.on("pointerup pointercancel lostpointercapture", () => {
      if (!dragging) return;
      dragging = false;
      document.documentElement.classList.remove("dragging");
      settled();
    });
    svg.on("selectstart dragstart", (event) => event.preventDefault());
    svg.on("pointerleave", () => {
      if (!dragging) hideTip();
    });
    svg.on("keydown", (event) => {
      const step = (event.shiftKey ? 7 : 1) * DAY;
      const moves = {
        ArrowLeft: [-step, 0],
        ArrowRight: [step, 0],
        ArrowUp: [0, step],
        ArrowDown: [0, -step],
      };
      const move = moves[event.key];
      if (!move) return;
      event.preventDefault();
      if (S.dims === 1 && move[1]) return;
      setCross(S.app + move[0], S.txn + move[1]);
      settled();
    });
  }

  function chip(kind) {
    const g = G.cross.append("g").attr("class", `chip ${kind}`);
    g.append("rect").attr("rx", 4).attr("height", 20);
    g.append("text").attr("dy", 14);
    return g;
  }

  function inPlot(px, py) {
    const slack = S.dims === 1 ? 24 : 6;
    return (
      px >= ML - 4 &&
      px <= W - MR + 4 &&
      py >= MT - slack &&
      py <= MT + plotH() + slack
    );
  }

  function moveTo(px, py) {
    const app = X.invert(clamp(px, ML, W - MR)).getTime();
    const txn =
      S.dims === 2 ? Y.invert(clamp(py, MT, MT + plotH())).getTime() : S.txn;
    setCross(app, txn);
  }

  function setCross(app, txn) {
    S.app = clamp(snap(app), snap(D.aLo), snap(D.aHi));
    S.txn = clamp(snap(txn), snap(D.tLo), D.today);
    drawCross(false);
    drawRects(false);
    renderReadout();
    checkPin();
  }

  function settled() {
    if (S.hoodOpen && S.hoodTab === "query") askServer();
  }

  function rectGeom(r) {
    const xa = X(Math.max(r.a0, D.aLo));
    const xb = X(Math.min(r.a1, D.aHi));
    let ya;
    let yb;
    if (S.dims === 2) {
      ya = Y(Math.min(r.t1, D.tHi));
      yb = Y(Math.max(r.t0, D.tLo));
    } else {
      ya = MT;
      yb = MT + STRIP_H;
    }
    return { x: xa, y: ya, w: Math.max(0, xb - xa), h: Math.max(0, yb - ya) };
  }

  function visible(r) {
    return S.dims === 2 || (r.t0 <= D.today && D.today < r.t1);
  }

  function measure() {
    const width = $("chart-wrap").clientWidth || 900;
    W = clamp(Math.round(width), 320, 1100);
    ML = W < 620 ? 78 : 120;
    PLANE_H = W < 620 ? 300 : 400;
  }

  function layout() {
    measure();
    D = domains();
    const h = plotH();
    X = d3.scaleUtc([D.aLo, D.aHi], [ML, W - MR]);
    Y = d3.scaleUtc([D.tLo, D.tHi], [MT + h, MT]);
    S.app = clamp(S.app, snap(D.aLo), snap(D.aHi));
    S.txn = clamp(S.txn, snap(D.tLo), D.today);
  }

  function draw(animate) {
    layout();
    const h = plotH();
    const t = (sel) =>
      animate ? sel.transition().duration(700).ease(d3.easeCubicInOut) : sel;

    t(svg).attr("viewBox", `0 0 ${W} ${MT + h + MB}`);
    [G.bg, G.clip].forEach((sel) =>
      t(sel)
        .attr("x", ML)
        .attr("y", MT)
        .attr("width", W - ML - MR)
        .attr("height", h),
    );
    const yToday = S.dims === 2 ? Y(D.today) : MT;
    t(G.future)
      .attr("x", ML)
      .attr("y", MT)
      .attr("width", W - ML - MR)
      .attr("height", Math.max(0, yToday - MT))
      .attr("opacity", S.dims === 2 ? 1 : 0);
    t(G.today.select("line"))
      .attr("x1", ML)
      .attr("x2", W - MR)
      .attr("y1", yToday)
      .attr("y2", yToday);
    G.today.select("text").text(`today · ${fmt(D.today)}`);
    t(G.today.select("text"))
      .attr("x", W - MR - 6)
      .attr("y", yToday - 5);
    t(G.today).attr("opacity", S.dims === 2 ? 1 : 0);
    const xToday = X(D.today);
    t(G.todayV.select("line"))
      .attr("x1", xToday)
      .attr("x2", xToday)
      .attr("y1", MT)
      .attr("y2", MT + h);
    G.todayV
      .select("text")
      .text(S.dims === 2 ? `today · ${fmt(D.today)}` : "today");
    // Rotated to run up the line, just left of it, from the bottom of the plot.
    // In the strip there's no room inside, so the label sits on top.
    G.todayV
      .select("text")
      .attr("text-anchor", S.dims === 2 ? "start" : "middle")
      .attr(
        "transform",
        S.dims === 2
          ? `translate(${xToday - 5},${MT + h - 6}) rotate(-90)`
          : `translate(${xToday},${MT - 5})`,
      );
    t(G.todayV).attr("opacity", 1);
    t(G.futureLabel)
      .attr("x", W - MR - 8)
      .attr("y", MT + 14)
      .attr("text-anchor", "end")
      .attr("opacity", S.dims === 2 && yToday - MT > 22 ? 1 : 0);

    const ticksX = Math.max(3, Math.round((W - ML - MR) / 90));
    t(G.ax)
      .attr("transform", `translate(0,${MT + h})`)
      .call(d3.axisBottom(X).ticks(ticksX).tickFormat(fmtTick).tickSizeOuter(0));
    t(G.ay)
      .attr("transform", `translate(${ML},0)`)
      .attr("opacity", S.dims === 2 ? 1 : 0)
      .call(d3.axisLeft(Y).ticks(7).tickFormat(fmtTick).tickSizeOuter(0));
    t(G.tx)
      .attr("x", ML + (W - ML - MR) / 2)
      .attr("y", MT + h + 46)
      .text(`When it was true in ${S.person}'s life →`);
    t(G.ty)
      .attr(
        "transform",
        `translate(${ML - (W < 620 ? 64 : 82)},${MT + h / 2}) rotate(-90)`,
      )
      .attr("opacity", S.dims === 2 ? 1 : 0)
      .text("When the bank believed it →");

    drawRects(animate);
    drawGhost(animate);
    drawCross(animate);
    renderClock();
  }

  function renderClock() {
    const today = D.today;
    $("clock-month").textContent = d3.utcFormat("%b %Y")(today);
    $("clock-day").textContent = d3.utcFormat("%-d")(today);
    $("clock-date").textContent = fmt(today);
    $("clock-note").textContent = S.ch.sandbox
      ? "The clock moves forward whenever you write on a later day."
      : S.dims === 1
        ? `The strip shows what the bank believes today. The dotted line marks today in ${S.person}'s life.`
        : (S.ch.tools || []).length
          ? "New writes are dated today. The dotted lines mark today on both axes."
          : "The dotted lines mark today on both axes.";
  }

  function drawRects(animate) {
    const chosen = believed(S.app, S.txn).row;
    const join = G.rects
      .selectAll("g.belief")
      .data(S.rows, (r) => r.key)
      .join(
        (enter) => {
          const g = enter
            .append("g")
            .attr("class", "belief")
            .attr("opacity", 0);
          g.append("rect").attr("class", "fill");
          g.append("rect").attr("class", "hatch");
          g.append("text").attr("class", "label").attr("dy", "0.35em");
          g.each(function (r) {
            const geo = rectGeom(r);
            d3.select(this)
              .selectAll("rect")
              .attr("x", geo.x)
              .attr("y", geo.y)
              .attr("width", geo.w)
              .attr("height", geo.h);
          });
          return g;
        },
        (update) => update,
        (exit) =>
          animate
            ? exit.transition().duration(400).attr("opacity", 0).remove()
            : exit.remove(),
      );

    join
      .classed("past", (r) => !r.current)
      .classed("chosen", (r) => chosen && r.key === chosen.key)
      .classed("hovered", (r) => S.hover === r.key);

    join.each(function (r) {
      const geo = rectGeom(r);
      const g = d3.select(this);
      const tr = (sel) =>
        animate ? sel.transition().duration(700).ease(d3.easeCubicInOut) : sel;
      tr(g).attr("opacity", visible(r) ? 1 : 0);
      tr(g.selectAll("rect"))
        .attr("x", geo.x)
        .attr("y", geo.y)
        .attr("width", geo.w)
        .attr("height", geo.h);
      g.select("rect.fill").attr("fill", colorOf(r.street));
      const fits = geo.w > r.street.length * 7.6 + 16 && geo.h > 20;
      tr(g.select("text"))
        .attr("x", geo.x + 8)
        .attr("y", geo.y + geo.h / 2)
        .attr("opacity", fits ? 1 : 0);
      g.select("text").text(r.street);
    });
  }

  function drawGhost(animate) {
    const ghost = S.ghost;
    const tr = (sel) => (animate ? sel.transition().duration(500) : sel);
    if (!ghost) {
      tr(G.ghost).attr("opacity", 0);
      return;
    }
    const geo = rectGeom({
      a0: ghost.a0,
      a1: ghost.a1,
      t0: ghost.t0,
      t1: Infinity,
    });
    tr(G.ghost)
      .attr("x", geo.x)
      .attr("y", geo.y)
      .attr("width", geo.w)
      .attr("height", geo.h)
      .attr("opacity", 1);
  }

  function drawCross(animate) {
    const h = plotH();
    const x = X(S.app);
    const y = S.dims === 2 ? Y(S.txn) : MT - 10;
    const tr = (sel) =>
      animate ? sel.transition().duration(700).ease(d3.easeCubicInOut) : sel;
    tr(G.vline)
      .attr("x1", x)
      .attr("x2", x)
      .attr("y1", S.dims === 2 ? MT : MT - 10)
      .attr("y2", MT + h);
    tr(G.hline)
      .attr("x1", ML)
      .attr("x2", W - MR)
      .attr("y1", S.dims === 2 ? y : MT + h / 2)
      .attr("y2", S.dims === 2 ? y : MT + h / 2)
      .attr("opacity", S.dims === 2 ? 1 : 0);
    tr(G.dot).attr("cx", x).attr("cy", y);

    placeChip(G.appChip, fmt(S.app), x, MT + h + 4, "middle", tr);
    placeChip(
      G.txnChip,
      S.txn === D.today ? `today, ${fmtShort(S.txn)}` : fmt(S.txn),
      ML - 6,
      (S.dims === 2 ? y : MT) - 10,
      "end",
      tr,
    );
    tr(G.txnChip).attr("opacity", S.dims === 2 ? 1 : 0);
  }

  function placeChip(g, label, x, y, anchor, tr) {
    g.select("text").text(label);
    const width = label.length * 7 + 14;
    let left = anchor === "middle" ? x - width / 2 : x - width;
    left = clamp(left, 2, W - width - 2);
    tr(g).attr("transform", `translate(${left},${y})`);
    g.select("rect").attr("width", width);
    g.select("text").attr("x", width / 2).attr("text-anchor", "middle");
  }

  function hoverAt(px, py, event) {
    if (!inPlot(px, py)) {
      hideTip();
      return;
    }
    const app = X.invert(px).getTime();
    const txn = S.dims === 2 ? Y.invert(py).getTime() : D.today;
    const hit = S.rows.find((r) => visible(r) && covers(r, app, txn));
    setHover(hit ? hit.key : null);
    if (!hit) {
      hideTip();
      return;
    }
    const tip = $("tip");
    const span = (a, b, ink) =>
      `<span class="${ink}">${isFinite(a) ? fmt(a) : "always"}</span> → ` +
      `<span class="${ink}">${isFinite(b) ? fmt(b) : "still"}</span>`;
    let html = `<b>${esc(hit.street)}</b><br>True in ${esc(S.person)}'s life: ${span(hit.a0, hit.a1, "app-ink")}`;
    if (S.dims === 2) {
      html += `<br>Believed by the bank: ${span(hit.t0, hit.t1, "txn-ink")}`;
    }
    if (S.ch.hood) {
      html += `<br><code>${hit.table}</code> · row ${hit.row_id}`;
    }
    tip.innerHTML = html;
    tip.hidden = false;
    const wrap = $("chart-wrap").getBoundingClientRect();
    const left = clamp(event.clientX - wrap.left + 14, 0, wrap.width - 260);
    tip.style.left = `${left}px`;
    tip.style.top = `${event.clientY - wrap.top + 14}px`;
  }

  function hideTip() {
    $("tip").hidden = true;
    setHover(null);
  }

  function setHover(key) {
    if (S.hover === key) return;
    S.hover = key;
    G.rects.selectAll("g.belief").classed("hovered", (r) => r.key === key);
    document
      .querySelectorAll("#hood-body tr[data-key]")
      .forEach((tr) => tr.classList.toggle("hovered", tr.dataset.key === key));
  }

  function setDims(n) {
    S.dims = n;
    if (n === 2) S.txn = D.today;
    draw(true);
    renderReadout();
  }

  // ------------------------------------------------------------ readout

  function renderReadout() {
    const { row, count } = believed(S.app, S.txn);
    const who = esc(S.person);
    const street = row ? `<b>${esc(row.street)}</b>` : null;
    let html;
    if (S.dims === 1) {
      html = street
        ? `On ${appDate(S.app)}, ${who} lived at ${street}.`
        : `The bank has no address for ${who} on ${appDate(S.app)}.`;
    } else {
      const asOf =
        S.txn === D.today
          ? `As of <span class="txn-ink">today (${fmt(S.txn)})</span>`
          : `As of ${txnDate(S.txn)}`;
      if (count > 1) {
        html = "More than one belief matches that pair of days.";
      } else if (street) {
        html = `${asOf}, the bank believed ${who} lived at ${street} on ${appDate(S.app)}.`;
      } else {
        html = `${asOf}, the bank had no address for ${who} on ${appDate(S.app)}.`;
      }
    }
    $("readout").innerHTML = html;
  }

  // ------------------------------------------------------------ story

  function currentGoal() {
    return S.ch.goals[S.goal] || null;
  }

  function renderStory() {
    const ch = S.ch;
    $("ch-kicker").textContent = ch.sandbox
      ? "Epilogue"
      : `Chapter ${S.index + 1} of ${CHAPTERS.length - 1}`;
    $("ch-title").textContent = ch.title;
    $("ch-intro").innerHTML = ch.intro.map((p) => `<p>${p}</p>`).join("");

    const list = $("goals");
    list.innerHTML = "";
    ch.goals.forEach((goal, i) => {
      if (i > S.goal) return;
      const li = document.createElement("li");
      if (i < S.goal) {
        li.className = "goal done";
        li.innerHTML =
          `<p class="goal-text">${goal.text}</p>` +
          (goal.why ? `<p class="why">${goal.why}</p>` : "");
      } else {
        li.className = "goal active";
        li.innerHTML = `<p class="goal-text">${goal.text}</p>`;
        li.appendChild(goalControls(goal));
      }
      list.appendChild(li);
    });
    list.hidden = !ch.goals.length;

    const finished = S.goal >= ch.goals.length && ch.goals.length > 0;
    $("ch-outro").hidden = !finished;
    $("ch-outro").innerHTML = finished ? `<p>${ch.outro}</p>` : "";
    const next = CHAPTERS[S.index + 1];
    $("next").hidden = !finished || !next;
    if (next) $("next").textContent = `Next: ${next.title} →`;
    $("restart").textContent = ch.sandbox ? "Back to the start" : "Start over";

    const goal = currentGoal();
    $("hood-toggle").classList.toggle("pulse", !!(goal && goal.hood && !S.hoodOpen));
    renderNudge();
    renderSteps();
  }

  function goalControls(goal) {
    const box = document.createElement("div");
    box.className = "controls";
    if (goal.kind === "quiz") {
      const row = document.createElement("div");
      row.className = "choices";
      goal.choices.forEach((choice) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "choice";
        b.textContent = choice;
        b.addEventListener("click", () => {
          if (choice === goal.answer) {
            completeGoal();
          } else {
            b.classList.remove("wrong");
            void b.offsetWidth;
            b.classList.add("wrong");
            S.feedback = goal.wrong || "Not quite. Check the chart.";
            showFeedback(box);
          }
        });
        row.appendChild(b);
      });
      box.appendChild(row);
    }
    if (goal.kind === "action") {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "primary";
      b.textContent = goal.button;
      b.addEventListener("click", () => {
        goal.run();
        completeGoal();
      });
      box.appendChild(b);
    }
    if (goal.hood && !S.hoodOpen) {
      const open = document.createElement("button");
      open.type = "button";
      open.className = "quiet open-hood";
      open.textContent = "Open Under the hood ↓";
      open.addEventListener("click", () => setHood(true));
      box.appendChild(open);
    }
    if (goal.hint) {
      const hint = document.createElement("button");
      hint.type = "button";
      hint.className = "hint-toggle";
      hint.textContent = S.showHint ? "Hide hint" : "Hint";
      hint.addEventListener("click", () => {
        S.showHint = !S.showHint;
        renderStory();
      });
      box.appendChild(hint);
      if (S.showHint) {
        const p = document.createElement("p");
        p.className = "hint";
        p.innerHTML = goal.hint;
        box.appendChild(p);
      }
    }
    showFeedback(box);
    return box;
  }

  function showFeedback(box) {
    let p = box.querySelector(".feedback");
    if (!S.feedback) {
      if (p) p.remove();
      return;
    }
    if (!p) {
      p = document.createElement("p");
      p.className = "feedback";
      box.appendChild(p);
    }
    p.innerHTML = S.feedback;
  }

  function completeGoal() {
    S.goal += 1;
    S.feedback = "";
    S.showHint = false;
    if (S.goal >= S.ch.goals.length && S.ch.goals.length) {
      progress.done[S.ch.id] = true;
      saveProgress();
    }
    renderStory();
    const active = document.querySelector(".goal.active, .outro:not([hidden])");
    if (active) active.classList.add("arrive");
  }

  function renderNudge() {
    const goal = currentGoal();
    let text = "";
    if (goal && goal.kind === "pin") {
      text =
        S.dims === 1 ? "Drag along the strip ↔" : "Drag the dot anywhere ↔ ↕";
    }
    $("nudge").textContent = text;
  }

  function checkPin() {
    const goal = currentGoal();
    if (!goal || goal.kind !== "pin") return;
    const { row } = believed(S.app, S.txn);
    if (goal.test({ app: S.app, txn: S.txn, street: row ? row.street : null })) {
      completeGoal();
    }
  }

  async function checkDb() {
    const goal = currentGoal();
    if (!goal || goal.kind !== "db") return;
    const results = await Promise.all(
      goal.probes.map(([app, txn]) =>
        api(
          `/api/ask?${new URLSearchParams({ person: S.person, app, txn })}`,
        ),
      ),
    );
    const miss = goal.probes.findIndex(
      (probe, i) => results[i].street !== probe[2],
    );
    if (miss < 0) {
      completeGoal();
      return;
    }
    const [app, txn, want] = goal.probes[miss];
    const got = results[miss].street;
    S.feedback =
      `Close. As of ${txnDate(day(txn))}, the bank should believe ${esc(S.person)} lived at <b>${esc(want)}</b> on ${appDate(day(app))}, ` +
      `but the ledger says ${got ? `<b>${esc(got)}</b>` : "nothing"}. ` +
      `Try again, or press <i>Start over</i> for a clean slate.`;
    S.showHint = true;
    renderStory();
  }

  function checkError(code) {
    const goal = currentGoal();
    if (goal && goal.kind === "error" && goal.code === code) completeGoal();
  }

  function renderSteps() {
    const nav = $("steps");
    nav.innerHTML = "";
    CHAPTERS.forEach((ch, i) => {
      const a = document.createElement("a");
      a.href = `#/${ch.id}`;
      a.className = "step";
      a.textContent = ch.sandbox ? "★" : String(i + 1);
      a.title = ch.title;
      if (progress.done[ch.id]) a.classList.add("done");
      if (S.ch && S.ch.id === ch.id && !$("game").hidden) {
        a.setAttribute("aria-current", "step");
      }
      nav.appendChild(a);
    });
  }

  // ------------------------------------------------------------ tools

  const form = $("write-form");

  function renderTools() {
    const ch = S.ch;
    const tools = ch.tools || [];
    $("tools").hidden = !tools.length;
    $("tool-tabs").hidden = tools.length < 2;
    $("sandbox").hidden = !ch.sandbox;
    document
      .querySelectorAll("[data-person]")
      .forEach((el) => (el.textContent = S.person));
    document.querySelectorAll("#tool-tabs [data-mode]").forEach((b) => {
      b.setAttribute("aria-selected", String(b.dataset.mode === S.mode));
    });
    form.querySelector('[data-for="record"]').hidden = S.mode !== "record";
    form.querySelector('[data-for="forget"]').hidden = S.mode !== "forget";
    $("write-button").textContent =
      S.mode === "record" ? "Write it in the ledger" : "Forget that period";
    form.street.required = S.mode === "record";
  }

  function fillForm() {
    const preset = S.ch.preset || {};
    form.street.value = preset.street || "";
    form.from.value = preset.from || "";
    form.until.value = "";
    form.still.checked = true;
    form.until.disabled = true;
    form.written.value =
      preset.written || isoDay(S.ch.sandbox ? todayMs() + DAY : todayMs());
    $("write-msg").textContent = "";
    $("write-msg").className = "form-msg";
    readGhost();
  }

  function readGhost() {
    const from = form.from.value;
    const written = form.written.value;
    const until = form.still.checked ? "" : form.until.value;
    if (!from || !written || (until && until <= from)) {
      S.ghost = null;
    } else {
      S.ghost = {
        a0: day(from),
        a1: until ? day(until) : Infinity,
        t0: day(written),
      };
    }
  }

  form.addEventListener("input", () => {
    form.until.disabled = form.still.checked;
    readGhost();
    if (S.ch) draw(true);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (S.busy) return;
    const body = {
      person: S.person,
      street: form.street.value,
      from: form.from.value,
      until: form.still.checked ? "" : form.until.value,
      written: form.written.value,
    };
    const msg = $("write-msg");
    S.busy = true;
    $("write-button").disabled = true;
    try {
      const path = S.mode === "record" ? "/api/record" : "/api/retract";
      const res = await api(path, body);
      if (res.sql && res.sql.length) S.lastWrite = { ...res, person: S.person };
      if (res.ok) {
        setRows(res.rows);
        S.people = res.people;
        const changes = diff(res.before, res.rows);
        msg.className = "form-msg ok";
        msg.textContent = `Written. ${summarize(changes)}`;
        if (S.ch.sandbox) form.written.value = isoDay(todayMs() + DAY);
        S.ghost = null;
        readGhost();
        draw(true);
        renderReadout();
        renderPeople();
        await checkDb();
      } else {
        msg.className = "form-msg bad";
        msg.textContent = res.error;
        if (S.ch.goals.length) checkError(res.code);
      }
      renderHood();
    } catch (error) {
      msg.className = "form-msg bad";
      msg.textContent = `Couldn't reach the server: ${error.message}`;
    } finally {
      S.busy = false;
      $("write-button").disabled = false;
    }
  });

  document.querySelectorAll("#tool-tabs [data-mode]").forEach((b) =>
    b.addEventListener("click", () => {
      S.mode = b.dataset.mode;
      renderTools();
    }),
  );

  function summarize(changes) {
    if (!changes.length) return "Nothing changed.";
    const count = (kind) => changes.filter((c) => c.kind === kind).length;
    const parts = [];
    const add = (n, one, many) => n && parts.push(`${n} ${n === 1 ? one : many}`);
    add(count("insert"), "row inserted", "rows inserted");
    add(count("update"), "row updated", "rows updated");
    add(count("gone"), "row deleted", "rows deleted");
    add(count("archive"), "old version kept in history", "old versions kept in history");
    return parts.join(", ") + ".";
  }

  // ------------------------------------------------------------ sandbox

  function renderPeople() {
    $("people").innerHTML = S.people
      .map((p) => `<option value="${esc(p)}"></option>`)
      .join("");
    $("person-input").value = S.person;
  }

  $("person-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = $("person-input").value.trim();
    if (!name) return;
    S.person = name;
    S.lastWrite = null;
    await loadBook();
    fillForm();
    draw(true);
    renderReadout();
    renderTools();
    renderHood();
  });

  $("reset-person").addEventListener("click", async () => {
    const res = await api("/api/reset", { person: S.person });
    setRows(res.rows);
    S.people = res.people;
    S.lastWrite = null;
    afterReset();
  });

  let confirmAll = false;
  $("reset-all").addEventListener("click", async () => {
    const button = $("reset-all");
    if (!confirmAll) {
      confirmAll = true;
      button.textContent = "Erase everyone? Click again";
      setTimeout(() => {
        confirmAll = false;
        button.textContent = "Reset every customer";
      }, 4000);
      return;
    }
    confirmAll = false;
    button.textContent = "Reset every customer";
    await api("/api/reset", {});
    S.lastWrite = null;
    await loadBook();
    afterReset();
  });

  function afterReset() {
    fillForm();
    draw(true);
    renderReadout();
    renderPeople();
    renderHood();
  }

  // ------------------------------------------------------------ hood

  function setHood(open) {
    S.hoodOpen = open;
    if (open && !progress.seenHood) {
      progress.seenHood = true;
      saveProgress();
    }
    renderHood();
    renderStory();
    if (open) {
      if (S.hoodTab === "query") askServer();
      $("hood").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  $("hood-toggle").addEventListener("click", () => setHood(!S.hoodOpen));

  document.querySelectorAll(".hood-tabs [data-tab]").forEach((b) =>
    b.addEventListener("click", () => {
      S.hoodTab = b.dataset.tab;
      renderHood();
      if (S.hoodTab === "query") askServer();
    }),
  );

  function diff(before, after) {
    const was = new Map(before.map((r) => [keyOf(r), r]));
    const now = new Map(after.map((r) => [keyOf(r), r]));
    const changes = [];
    now.forEach((r, k) => {
      if (!was.has(k)) {
        changes.push({ kind: r.table === CURRENT ? "insert" : "archive", row: r });
      } else if (JSON.stringify(was.get(k)) !== JSON.stringify(r)) {
        changes.push({ kind: "update", row: r, was: was.get(k) });
      }
    });
    was.forEach((r, k) => {
      if (!now.has(k)) changes.push({ kind: "gone", row: r });
    });
    return changes;
  }

  function range(pair) {
    return `[${pair[0] || ""},${pair[1] || ""})`;
  }

  function human(pair) {
    const a = pair[0] ? fmt(day(pair[0])) : "always";
    const b = pair[1] ? fmt(day(pair[1])) : "open";
    return `${a} → ${b}`;
  }

  function narrate(changes) {
    const gone = new Set(
      changes.filter((c) => c.kind === "gone").map((c) => c.row.row_id),
    );
    const order = { archive: 0, gone: 1, update: 2, insert: 3 };
    return changes
      .slice()
      .sort((a, b) => order[a.kind] - order[b.kind])
      .map((c) => {
        const r = c.row;
        const street = `<b>${esc(r.street)}</b>`;
        if (c.kind === "archive") {
          const why = gone.has(r.row_id) ? "was deleted" : "was updated";
          return (
            `<li><span class="tag archive">trigger</span> Row ${r.row_id} ${why}, so ` +
            `<code>record_txn_history</code> first copied the old version (${street}, ` +
            `true <span class="app-ink">${human(r.app)}</span>) into <code>${HISTORY}</code>, ` +
            `with its belief closed at <span class="txn-ink">${fmt(day(r.txn[1]))}</span>.</li>`
          );
        }
        if (c.kind === "gone") {
          return `<li><span class="tag gone">delete</span> Row ${r.row_id} (${street}) left <code>${CURRENT}</code>. Its whole period was covered by the new write.</li>`;
        }
        if (c.kind === "update") {
          return (
            `<li><span class="tag update">update</span> Row ${r.row_id} (${street}) was trimmed from ` +
            `<span class="app-ink">${human(c.was.app)}</span> to <span class="app-ink">${human(r.app)}</span>. ` +
            `Its belief now starts at <span class="txn-ink">${fmt(day(r.txn[0]))}</span>.</li>`
          );
        }
        return (
          `<li><span class="tag insert">insert</span> Row ${r.row_id}: ${street}, ` +
          `true <span class="app-ink">${human(r.app)}</span>, believed from ` +
          `<span class="txn-ink">${fmt(day(r.txn[0]))}</span>.</li>`
        );
      })
      .join("");
  }

  function sqlBlock(lines) {
    const keywords =
      /\b(SELECT|FROM|WHERE|AND|OR|INSERT|INTO|VALUES|UPDATE|SET|DELETE|RETURNING|BEGIN|COMMIT|ROLLBACK|AS|ON|IN|NULL)\b/g;
    const operators = /(@&gt;|&amp;&amp;)/g;
    return (
      `<pre class="sql">` +
      lines
        .map((line) =>
          esc(line)
            .replace(keywords, "<b>$1</b>")
            .replace(operators, '<em class="op">$1</em>'),
        )
        .join("\n\n") +
      `</pre>`
    );
  }

  function tableHtml(name, rows, marks, removed) {
    const head =
      "<tr><th>row_id</th><th>person</th><th>street</th>" +
      "<th class='app-ink'>app_period</th><th class='txn-ink'>txn_period</th><th></th></tr>";
    const body = rows
      .map((r) => {
        const mark = marks.get(keyOf(r)) || "";
        const tag = mark ? `<span class="tag ${mark}">${mark}</span>` : "";
        const hovered = S.hover === keyOf(r) ? " hovered" : "";
        return (
          `<tr data-key="${keyOf(r)}" class="${mark}${hovered}">` +
          `<td>${r.row_id}</td><td>${esc(r.person)}</td><td>${esc(r.street)}</td>` +
          `<td><code>${range(r.app)}</code></td><td><code>${range(r.txn)}</code></td><td>${tag}</td></tr>`
        );
      })
      .concat(
        removed.map(
          (r) =>
            `<tr class="gone"><td>${r.row_id}</td><td>${esc(r.person)}</td><td>${esc(r.street)}</td>` +
            `<td><code>${range(r.app)}</code></td><td><code>${range(r.txn)}</code></td>` +
            `<td><span class="tag gone">deleted</span></td></tr>`,
        ),
      )
      .join("");
    const empty = rows.length || removed.length ? "" : `<tr><td colspan="6" class="empty">No rows.</td></tr>`;
    return (
      `<div class="table-box"><h3><code>${name}</code> <span>${rows.length} row${rows.length === 1 ? "" : "s"}</span></h3>` +
      `<div class="scroll"><table><thead>${head}</thead><tbody>${body}${empty}</tbody></table></div></div>`
    );
  }

  function renderHood() {
    const ch = S.ch;
    const show = !!(ch && ch.hood);
    $("hood").hidden = !show;
    $("hood").classList.toggle("open", S.hoodOpen);
    $("hood-toggle").setAttribute("aria-expanded", String(S.hoodOpen && show));
    $("hood-panel").hidden = !(show && S.hoodOpen);
    $("hood-new").hidden = !!progress.seenHood;
    if (!show || !S.hoodOpen) return;
    document.querySelectorAll(".hood-tabs [data-tab]").forEach((b) => {
      b.setAttribute("aria-selected", String(b.dataset.tab === S.hoodTab));
    });

    const last =
      S.lastWrite && S.lastWrite.person === S.person ? S.lastWrite : null;
    const changes = last && last.ok ? diff(last.before, last.rows) : [];
    const marks = new Map();
    changes.forEach((c) => {
      if (c.kind !== "gone") marks.set(keyOf(c.row), c.kind === "archive" ? "archived" : c.kind);
    });
    const removed = changes
      .filter((c) => c.kind === "gone")
      .map((c) => c.row);
    const raw = S.rows;
    let html = "";

    if (S.hoodTab === "tables") {
      html =
        `<p class="explain">These are the two real Postgres tables behind the chart. ` +
        `<code>${CURRENT}</code> holds what the bank believes now. <code>${HISTORY}</code> holds every belief it has replaced. ` +
        `A range like <code>[2022-01-01,2022-06-13)</code> includes its first day and excludes its last; ` +
        `a blank end means it's still open. Hover a row to find its block.</p>` +
        `<div class="tables">` +
        tableHtml(CURRENT, raw.filter((r) => r.current), marks, removed) +
        tableHtml(HISTORY, raw.filter((r) => !r.current), marks, []) +
        `</div>`;
    } else if (S.hoodTab === "write") {
      if (!last) {
        html = `<p class="explain">Make a write and this tab replays it step by step: the SQL that was sent, and what the trigger did inside Postgres.</p>`;
      } else {
        html =
          (last.ok
            ? `<h3>What changed</h3><ol class="story-of-write">${narrate(changes) || "<li>Nothing changed.</li>"}</ol>`
            : `<h3>Rejected</h3><p class="form-msg bad">${esc(last.error)}</p><p class="explain">The transaction was rolled back, so nothing changed.</p>`) +
          `<h3>SQL sent to Postgres</h3>` +
          `<p class="explain"><code>set_transaction_time</code> pins the transaction's clock to the “written down on” date. ` +
          `<code>Writer.clear_key_period</code> then trims, splits, or deletes whatever overlaps the new period. ` +
          `The trigger's own work runs inside Postgres, so it doesn't show up here. That's the list above.</p>` +
          sqlBlock(last.sql);
      }
    } else {
      const q = S.lastAsk;
      html =
        `<p class="explain">The sentence under the chart is worked out in your browser. This is the same question sent to the database with <code>Writer.as_of</code>, which searches both tables. ` +
        `<code>@&gt;</code> means “range contains”.</p>` +
        (q
          ? `<p class="answer-line">As of <span class="txn-ink">${fmt(day(q.txn))}</span>, about <span class="app-ink">${fmt(day(q.app))}</span>: ` +
            `<b>${q.street ? esc(q.street) : "no row"}</b></p>` +
            sqlBlock(q.sql)
          : `<p class="explain">Asking…</p>`);
    }
    $("hood-body").innerHTML = html;
    document.querySelectorAll("#hood-body tr[data-key]").forEach((tr) => {
      tr.addEventListener("mouseenter", () => setHover(tr.dataset.key));
      tr.addEventListener("mouseleave", () => setHover(null));
    });
  }

  let askToken = 0;
  async function askServer() {
    const token = ++askToken;
    const app = isoDay(S.app);
    const txn = isoDay(S.txn);
    const res = await api(
      `/api/ask?${new URLSearchParams({ person: S.person, app, txn })}`,
    );
    if (token !== askToken) return;
    S.lastAsk = { ...res, app, txn };
    if (S.hoodTab === "query") renderHood();
  }

  // ------------------------------------------------------------ flow

  async function loadBook() {
    const res = await api(
      `/api/book?${new URLSearchParams({ person: S.person })}`,
    );
    setRows(res.rows);
    S.people = res.people;
  }

  async function startChapter(index) {
    const ch = CHAPTERS[index];
    S.ch = ch;
    S.index = index;
    S.person = ch.person;
    S.goal = 0;
    S.feedback = "";
    S.showHint = false;
    S.mode = "record";
    S.lastWrite = null;
    S.lastAsk = null;
    S.dims = ch.dims;
    S.ghost = null;
    $("home").hidden = true;
    $("game").hidden = false;
    try {
      if (ch.sandbox) {
        await loadBook();
      } else {
        const res = await api("/api/reset", { person: ch.person });
        setRows(res.rows);
        S.people = res.people;
      }
    } catch (error) {
      $("ch-intro").innerHTML = `<p class="form-msg bad">Couldn't reach the server: ${esc(error.message)}</p>`;
      return;
    }
    D = domains();
    S.app = day(ch.start.app);
    S.txn = D.today;
    fillForm();
    renderTools();
    renderPeople();
    renderStory();
    G.rects.selectAll("g.belief").remove();
    draw(false);
    renderReadout();
    renderHood();
    window.scrollTo({ top: 0 });
  }

  function renderHome() {
    $("home").hidden = false;
    $("game").hidden = true;
    S.ch = null;
    const list = $("cases");
    list.innerHTML = "";
    const firstOpen = CHAPTERS.findIndex((ch) => !progress.done[ch.id]);
    CHAPTERS.forEach((ch, i) => {
      const li = document.createElement("li");
      const done = progress.done[ch.id];
      li.className = "case" + (done ? " done" : "") + (i === firstOpen ? " next" : "");
      li.innerHTML =
        `<a href="#/${ch.id}"><span class="num">${ch.sandbox ? "★" : i + 1}</span>` +
        `<span class="what"><b>${esc(ch.title)}</b><span>${esc(ch.teaser)}</span></span>` +
        `<span class="state">${done ? "✓ done" : i === firstOpen ? "start →" : ""}</span></a>`;
      list.appendChild(li);
    });
    renderSteps();
  }

  function route() {
    const id = location.hash.replace(/^#\/?/, "");
    const index = CHAPTERS.findIndex((ch) => ch.id === id);
    if (index < 0) renderHome();
    else startChapter(index);
  }

  $("restart").addEventListener("click", () => {
    if (S.ch.sandbox) location.hash = "#/";
    else startChapter(S.index);
  });
  $("next").addEventListener("click", () => {
    location.hash = `#/${CHAPTERS[S.index + 1].id}`;
  });

  window.addEventListener("hashchange", route);
  let resizing = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizing);
    resizing = setTimeout(() => S.ch && draw(false), 120);
  });
  setupChart();
  route();
})();
