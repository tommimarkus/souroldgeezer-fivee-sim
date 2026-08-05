/* The Play-mode driver for the map editor: one fight, run from a browser.

   One global namespace, FiveePlay, and two calls — start(context) on entering
   Play, stop() on leaving it. Everything this file touches arrives in that
   context: the element it renders into, the scene to run, the request helper,
   the renderer, the canvas, a hit test, and the two output channels. It looks
   nothing up. There is no getElementById here and no reference to
   FiveeRenderer, because a driver that reached for either would be a second
   file agreeing with editor.html's markup by convention, and the next page to
   host it would have to reproduce that markup to work at all.

   Its look travels with it, for the same reason. editor.html styles the shell
   it lends out and says in its own comment that what fills #play-root is this
   file's business, so the stylesheet is here — injected once into
   `document.head`, which is the one handle this file is not given and is not a
   lookup of anybody's markup. It declares no colour of its own: every value is
   one of the host page's custom properties or a mix of two, so the panel
   follows whatever page it is standing in, light or dark.

   No network of its own, no fonts, no external references: every request goes
   through the helper it was handed, on the origin that served this file.

   WHAT THE PLAYER CHAIR IS, AND WHAT IT IS NOT

   Two seats read the same fight through two doors. The whole table's chair
   reads encounter.state, which reports everything and is what a person running
   the fight needs. A player's chair reads encounter.brief?as=<name>, which is
   the allowlist projection Encounter.brief() carries in model/encounter.py:
   their own sheet whole, their allies' sheets whole beside it, an opponent
   reduced to where it is standing and how badly hurt it looks in words, and an
   undetected creature absent altogether.

   The two doors answer two *shapes*, and the second one is the better of the
   two to draw from. encounter.state is one flat `combatants` list in initiative
   order. A brief arrives already sorted into `you`, `allies` and `enemies`, and
   the enemy entries carry two facts the flat list never had: how far off they
   are, and a plain-language `health` band where their hit points would be.
   cast() is the one place that knows both shapes, and it keeps what the brief
   adds rather than flattening it back into what the other door answers.

   This is not a permission system, and nothing here should be read as one.
   `as=` is asserted by the caller and authenticated by nothing — this engine
   has one per-launch token and no per-seat credential — so a client that can
   ask for a player's brief can equally ask for encounter.state and be given
   the whole fight. What the chair buys is an honest data path: a browser
   sitting in a player's seat is never sent the numbers it would then have to
   remember not to draw, which is the failure client-side hiding actually has.
   Against somebody who does not want to cooperate it buys nothing, and it is
   not offered as though it did.

   A write carries the chair as well as a read. encounter.create, encounter.act
   and encounter.advance each take the same `as=`, so the answer to a player's
   own post arrives already projected — in the brief's own shape, not a second
   redacted one — and this file is never handed what it would then have to
   remember not to draw. It used to be: those three answered
   the acting fight's *full* state whichever chair posted it, and the driver
   survived that only by reading `events` out of the answer and discarding the
   rest — hiding in the browser, which is inert against anything that reads the
   response before this file does.

   What remains is that the driver still re-reads through its own door after
   every write, and that is now for freshness rather than for secrecy: the
   answer to a post is a fight one action old the moment anybody else acts.
   Exactly one place decides what a seat may hold, and it is seatQuery(). */
"use strict";
var FiveePlay = (function () {
  /* --- the routes, as web/routes.py declares them ----------------------- */
  /* Relative to the injected apiBase, which the page's request helper
     prepends. tests/test_web_assets.py holds every one of these against the
     route table, so a route that moves fails there rather than 404ing here
     halfway through somebody's fight. */
  var OPENAPI = "/openapi.json";
  var ENCOUNTERS = "/encounters";
  var BRIEF = "/brief";
  var ACTIONS = "/actions";
  var ADVANCE = "/advance";
  /* The operationId of the route whose request body declares the action kinds.
     routes.py derives it from the operation name, so this string identifies a
     route rather than restating one. */
  var ACT_OPERATION = "encounterAct";

  /* --- what a kind wants a click for ------------------------------------- */
  /* NOT the set of kinds. That comes from the contract this launch serves, and
     a kind absent from this table still gets a button and is still posted —
     see actionsFor(). What this says is only what *this interface* has to ask
     for before it can send one: a creature to aim at, a square to walk to, or
     a fixture to work. Anything else is complete as soon as it is chosen. */
  var CLICK_FOR = {
    "attack": "creature",
    "cast": "creature",
    "use_item": "creature",
    "move": "square",
    "interact": "fixture"
  };

  /* Feet per square. The engine's own constant (kernel/grid.py), and the
     divisor every other page already uses to turn a reported position into a
     square: a fight's positions are feet and the renderer's tokens are
     squares, and confusing the two puts the whole party in one corner. */
  var FEET_PER_SQUARE = 5;
  /* How long the die turns for. Nothing depends on the number being this one;
     what depends on it is that the face is not known until it stops. */
  var TUMBLE_MS = 720;

  /* --- state ------------------------------------------------------------- */
  var ctx = null;           /* the context start() was handed */
  var el = null;            /* the elements this driver built */
  var kinds = null;         /* the action kinds, as the contract answered */
  var kindsNote = "";       /* why there are none, when there are none */
  var encounterId = null;
  var version = null;       /* the ETag last read: this fight's journal head */
  var snapshot = null;      /* the fight, as this chair may see it */
  var seat = "";            /* "" is the whole table; otherwise a combatant */
  var armed = null;         /* {kind, wants} while waiting for a click */
  var tumbling = 0;         /* a generation, so a stopped driver settles nothing */
  var onCanvasClick = null;

  /* --- small helpers ----------------------------------------------------- */
  function make(tag, id, className) {
    var node = document.createElement(tag);
    if (id) { node.id = id; }
    if (className) { node.className = className; }
    return node;
  }
  function clear(node) {
    while (node.children.length) { node.removeChild(node.children[0]); }
    node.textContent = "";
  }
  function say(text, cls) { ctx.setStatus(text, cls || ""); }
  /* The engine's own sentence, never a paraphrase and never just a status: a
     refusal names the rule that produced it, and this file knows no rules. */
  function refusal(response, fallback) {
    var detail = response && response.json && response.json.detail;
    say(detail ? String(detail) : fallback + " (" + response.status + ")", "error");
  }
  function feetToCell(point) {
    return [
      Math.floor(point[0] / FEET_PER_SQUARE),
      Math.floor(point[1] / FEET_PER_SQUARE)
    ];
  }
  function cellToFeet(cell) {
    return [cell[0] * FEET_PER_SQUARE, cell[1] * FEET_PER_SQUARE];
  }
  function labelOf(spec) {
    return String(spec.name || spec.creature || spec.monster || spec.label || "unnamed");
  }
  function encounterPath() { return ENCOUNTERS + "/" + encodeURIComponent(encounterId); }
  function versionHeaders() {
    return version ? { "If-Match": version } : {};
  }
  function remember(response) {
    var tag = response.headers && response.headers.get
      ? response.headers.get("ETag") : null;
    if (tag) { version = tag; }
  }

  /* --- the look ----------------------------------------------------------- */
  /* The driver's own stylesheet, and it lives here for the reason its markup
     does. editor.html styles the shell it lends out — `#panel, #play-panel` and
     the column's headings — and says in its own comment that what fills
     #play-root is this file's business. A look kept in the page would be the
     other half of that split living in the wrong file, and the next page to
     host this driver would have to reproduce a stylesheet to make it legible.

     **No colour is declared.** Every value below is one of the host page's own
     custom properties or a mix of two, which is what makes this panel follow
     the page into dark mode with no second block: there is no light-mode hex
     here that could be wrong at night. It is also the whole of "match the
     visual language" — the driver has no palette, it borrows the one it is
     standing in.

     **Nothing here draws a proportion.** A player's chair is sent an
     opponent's condition in words exactly so the browser never holds the
     ratio, so the panel's quantities are pips — one per square of movement,
     one per attack — which are counts drawn as counts. A bar with a
     denominator behind it would teach the eye to read back the number the
     projection spent its design withholding. */
  var STYLE = [
    /* The column, and the two tints everything accent-coloured is mixed from.
       Declared once here and inherited, so a theme change moves one value. */
    ".fivee-play {",
    "  display: flex; flex-direction: column; gap: 8px;",
    "  --play-wash: color-mix(in srgb, var(--accent) 15%, transparent);",
    "  --play-line: color-mix(in srgb, var(--accent) 34%, transparent);",
    "  --play-glow: color-mix(in srgb, var(--accent) 42%, transparent);",
    "}",
    /* Stations, divided by the panel's own hairline rather than by boxes: this
       column already sits inside a bordered aside, and a card inside a card is
       one border too many. Three rules, three questions — what is happening,
       what may I do, what did the die say. */
    ".fivee-play > .play-round,",
    ".fivee-play > .play-actions,",
    ".fivee-play > .play-dice {",
    "  border-top: 1px solid var(--edge); padding-top: 8px;",
    "}",
    ".fivee-play :focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }",

    /* The fight's name and seed. Monospaced like every other block in this
       column that is read rather than clicked, and gone entirely before there
       is a fight to name — an empty line would leave a gap that means nothing. */
    ".play-note-line {",
    "  font-family: ui-monospace, monospace; font-size: 11px;",
    "  color: var(--muted); overflow-wrap: anywhere;",
    "}",
    ".play-note-line:empty { display: none; }",

    /* Taking a chair, naming a seed, and starting. */
    ".play-row { display: flex; align-items: center; gap: 6px; }",
    ".play-setup { flex-wrap: wrap; }",
    ".play-seat { flex: 1 1 7em; min-width: 0; }",
    ".play-seed {",
    "  flex: 0 1 5.5em; min-width: 0; text-align: center;",
    "  font-family: ui-monospace, monospace; font-variant-numeric: tabular-nums;",
    "}",
    /* Exactly one filled button is on screen at a time, and which one it is
       says where the fight has got to: Play until it starts, End turn from
       then on. A disabled Play does not stay dressed as the thing to press. */
    ".play-start {",
    "  flex: 0 0 auto; font-weight: 600;",
    "  background: var(--accent); border-color: var(--accent); color: var(--bg);",
    "}",
    ".play-start:hover:not(:disabled) { filter: brightness(1.12); }",
    ".play-start:disabled {",
    "  background: var(--panel); border-color: var(--edge); color: var(--muted);",
    "}",

    /* The round and whose turn it is: the one sentence in this panel set as a
       headline rather than as data. */
    ".play-round { font-size: 15px; font-weight: 600; line-height: 1.3; }",
    ".play-round.is-over { color: var(--accent); font-weight: 700; }",

    /* The initiative rail. The turn marker is a filled bar down the left edge
       and not a bullet or a colour change alone: the rail is read as a column,
       and a filled edge is the one thing in a column of small grey type that
       the eye lands on without being asked to look. */
    ".play-order { display: flex; flex-direction: column; gap: 2px; }",
    ".play-seat-row {",
    "  position: relative; padding: 4px 6px 4px 13px; border-radius: 4px;",
    "  border: 1px solid transparent; color: var(--muted);",
    "  font-family: ui-monospace, monospace; font-size: 11px;",
    "  font-variant-numeric: tabular-nums; overflow-wrap: anywhere;",
    "}",
    ".play-seat-row::before {",
    "  content: ''; position: absolute; left: 4px; top: 4px; bottom: 4px;",
    "  width: 3px; border-radius: 2px; background: transparent;",
    "}",
    ".play-seat-row.play-current {",
    "  color: var(--text); font-weight: 700;",
    "  background: var(--bg);",
    "  background: var(--play-wash);",
    "  border-color: var(--play-line);",
    "}",
    ".play-seat-row.play-current::before { background: var(--accent); }",

    /* The turn budget. Two counts and three tokens, each carrying its state in
       something other than the word for it. */
    ".play-budget { display: flex; flex-direction: column; gap: 5px; }",
    ".play-budget.is-resting {",
    "  color: var(--muted); font-size: 11px; font-style: italic;",
    "}",
    ".play-gauge { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }",
    ".play-gauge-text {",
    "  font-family: ui-monospace, monospace; font-size: 11px;",
    "  font-variant-numeric: tabular-nums; color: var(--text);",
    "}",
    ".play-gauge[data-state='spent'] .play-gauge-text { color: var(--muted); }",
    ".play-gauge-track { display: flex; gap: 2px; flex-wrap: wrap; min-width: 0; }",
    /* Two shapes, because two quantities. A square of ground and a strike are
       not the same unit and should not read as the same row of dots. */
    ".play-pip { flex: none; background: var(--accent); }",
    ".play-pip-move { width: 7px; height: 7px; border-radius: 1px; }",
    ".play-pip-attack {",
    "  width: 6px; height: 6px; margin: 1px 2px; transform: rotate(45deg);",
    "}",
    ".play-chips { display: flex; flex-direction: column; gap: 3px; }",
    /* Held or spent, as a token you still have or a socket you emptied. */
    ".play-chip {",
    "  display: flex; align-items: center; gap: 6px; color: var(--text);",
    "  font-family: ui-monospace, monospace; font-size: 11px;",
    "}",
    ".play-chip::before {",
    "  content: ''; flex: none; width: 8px; height: 8px; border-radius: 50%;",
    "  border: 1px solid var(--accent); background: var(--accent);",
    "}",
    ".play-chip[data-state='spent'] { color: var(--muted); }",
    ".play-chip[data-state='spent']::before {",
    "  background: transparent; border-color: var(--edge);",
    "}",

    /* The bar. Ten kinds in a 240px column, so they wrap and stay small. */
    ".play-actions { display: flex; flex-wrap: wrap; gap: 4px; }",
    ".play-action {",
    "  position: relative; flex: 1 1 auto; padding: 4px 7px;",
    "  font-size: 12px; line-height: 1.25; white-space: nowrap;",
    "}",
    /* The dot is CLICK_FOR, drawn. It is the difference between a kind that
       happens the moment you press it and one that then asks you to point at
       something, which is worth knowing before the press rather than after. */
    ".play-action[data-wants]::after {",
    "  content: ''; position: absolute; top: 3px; right: 3px;",
    "  width: 4px; height: 4px; border-radius: 50%; background: var(--accent);",
    "}",
    ".play-action:disabled::after { background: var(--muted); }",
    ".play-action.is-armed {",
    "  background: var(--accent); border-color: var(--accent);",
    "  color: var(--bg); font-weight: 600;",
    "  animation: play-armed 1.4s ease-in-out infinite;",
    "}",
    ".play-action.is-armed::after { background: var(--bg); }",
    "@keyframes play-armed {",
    "  0% { box-shadow: 0 0 0 0 var(--play-glow); }",
    "  70% { box-shadow: 0 0 0 5px transparent; }",
    "  100% { box-shadow: 0 0 0 0 transparent; }",
    "}",

    /* What to do next, and — while a kind is armed — that the battlefield is
       waiting on a click. The marker is drawn rather than written so that a
       screen reader is told the sentence and not a piece of punctuation. */
    ".play-hint {",
    "  display: flex; align-items: flex-start; gap: 6px;",
    "  font-size: 12px; color: var(--muted); min-height: 1.4em;",
    "}",
    ".play-hint::before {",
    "  content: ''; flex: none; width: 6px; height: 6px; margin-top: 5px;",
    "  border-radius: 50%; background: var(--edge);",
    "}",
    ".play-hint.is-armed { color: var(--accent); font-weight: 600; }",
    ".play-hint.is-armed::before {",
    "  background: var(--accent); box-shadow: 0 0 0 3px var(--play-wash);",
    "}",

    /* --- the die ---------------------------------------------------------
       The one shape in this panel, and the only place any boldness is spent.
       A d20 seen face-on has a hexagonal outline with a triangular face at its
       middle, so that is what this is: one clip-path for the silhouette and a
       second, behind the number, for the face it is printed on. No image,
       because a page that must work with no network cannot fetch a picture of
       a die — and drawing it is truer to the object anyway.

       The grid areas are what let the die sit above the controls that feed it
       while staying last in the markup. play.js builds the checkbox and the
       box before the thing they act on, and a driver that reordered its own
       DOM to suit a stylesheet would be letting the look decide the structure. */
    ".play-dice {",
    "  display: grid; grid-template-columns: auto auto auto;",
    "  grid-template-areas: 'die die die' 'own label face';",
    "  justify-content: center; align-items: center; gap: 8px 6px;",
    "}",
    /* The checkbox opts out of the page's blanket input styling, which would
       otherwise draw a bordered box around the box — the same line
       `.fixture-row input` takes in editor.html, for the same reason. */
    ".play-roll-own {",
    "  grid-area: own; flex: none; padding: 0; border: none; background: none;",
    "}",
    ".play-roll-label { grid-area: label; font-size: 12px; color: var(--muted); }",
    ".play-face {",
    "  grid-area: face; width: 4.5em; text-align: center;",
    "  font-family: ui-monospace, monospace; font-variant-numeric: tabular-nums;",
    "}",
    ".play-face:disabled { opacity: 0.45; }",
    ".play-die {",
    "  grid-area: die; justify-self: center;",
    "  position: relative; isolation: isolate;",
    "  width: 84px; height: 84px; padding: 3px 8px 0;",
    "  display: grid; place-items: center;",
    "  clip-path: polygon(50% 0%, 96% 26%, 96% 74%, 50% 100%, 4% 74%, 4% 26%);",
    "  background: var(--edge);",
    "  background: linear-gradient(158deg,",
    "    color-mix(in srgb, var(--accent) 24%, var(--panel)),",
    "    color-mix(in srgb, var(--edge) 75%, var(--panel)));",
    "  color: var(--text); font-family: ui-monospace, monospace;",
    "  font-size: 19px; font-weight: 700; line-height: 1;",
    "  font-variant-numeric: tabular-nums; text-align: center;",
    "  user-select: none; -webkit-user-select: none;",
    "}",
    /* The printed face. z-index puts it under the number, and `isolation` on
       the die keeps that -1 inside this box rather than sliding it behind the
       panel the die is sitting on. */
    ".play-die::before {",
    "  content: ''; position: absolute; z-index: -1;",
    "  left: 8%; right: 8%; top: 12%; bottom: 20%;",
    "  clip-path: polygon(50% 0%, 100% 100%, 0% 100%);",
    "  background: var(--bg);",
    "}",
    /* Three states, and each says something different. Idle: nothing has been
       rolled, so the die names itself and stays quiet. A pair: advantage was
       rolled with two dice and both faces are shown, so the type steps down to
       fit them. Settled: this is the face that was sent, and it is lit. */
    ".play-die[data-faces='idle'] {",
    "  font-size: 15px; font-weight: 600; letter-spacing: 0.06em;",
    "  color: var(--muted);",
    "}",
    ".play-die[data-faces='pair'] { font-size: 14px; letter-spacing: -0.01em; }",
    ".play-die.is-rolling {",
    "  animation: play-die-tumble 380ms linear infinite;",
    "  filter: drop-shadow(0 2px 4px var(--play-glow));",
    "}",
    ".play-die.is-settled {",
    "  animation: play-die-land 320ms cubic-bezier(0.2, 0.9, 0.3, 1) 1;",
    "  color: var(--accent);",
    "  filter: drop-shadow(0 0 5px var(--play-glow));",
    "}",
    ".play-die.is-settled::before {",
    "  background: color-mix(in srgb, var(--accent) 12%, var(--bg));",
    "}",
    /* A hexagon maps onto itself every 60 degrees, so the silhouette holds
       still while the printed face turns inside it — which is what a die
       tumbling towards you actually looks like. */
    "@keyframes play-die-tumble {",
    "  0% { transform: rotate(0deg) translateY(0) scale(1); }",
    "  25% { transform: rotate(120deg) translateY(-3px) scale(1.04); }",
    "  50% { transform: rotate(180deg) translateY(0) scale(0.98); }",
    "  75% { transform: rotate(300deg) translateY(-2px) scale(1.03); }",
    "  100% { transform: rotate(360deg) translateY(0) scale(1); }",
    "}",
    "@keyframes play-die-land {",
    "  0% { transform: scale(1.18); }",
    "  60% { transform: scale(0.97); }",
    "  100% { transform: scale(1); }",
    "}",

    /* Ending a turn is the panel's most-pressed control once a fight is on,
       and it is the filled one from then until the fight is over. */
    ".play-advance { width: 100%; padding: 6px 10px; font-weight: 600; }",
    ".play-advance:not(:disabled) {",
    "  background: var(--accent); border-color: var(--accent); color: var(--bg);",
    "}",
    ".play-advance:not(:disabled):hover { filter: brightness(1.12); }",

    /* Motion is how the roll reads, so it is the first thing to go. The die
       still changes its number every frame under this — that is the driver
       reporting what it is doing, not decoration — and the settled face is
       still lit, because colour is not movement. */
    "@media (prefers-reduced-motion: reduce) {",
    "  .play-die.is-rolling, .play-die.is-settled, .play-action.is-armed {",
    "    animation: none;",
    "  }",
    "}"
  ].join("\n");

  /* Injected once per page, and this flag is the whole of "once": the module
     is evaluated when the page loads the driver, so a second entry into Play
     finds the sheet already standing.

     `document.head` is the one handle here that was not passed in, and it is
     not a lookup of anybody's markup — every document has one, and a <style>
     has nowhere else to go. Nothing in this function reads the page. */
  var styled = false;
  function injectStyle() {
    if (styled) { return; }
    styled = true;
    var sheet = make("style", "play-style");
    sheet.textContent = STYLE;
    document.head.appendChild(sheet);
  }

  /* --- the panel --------------------------------------------------------- */
  function build(root) {
    el = {};
    root.classList.add("fivee-play");
    el.note = make("div", "play-note-line", "play-note-line");
    el.setup = make("div", null, "play-row play-setup");
    el.seat = make("select", "play-seat", "play-seat");
    el.seat.title = "whose chair this is";
    el.seed = make("input", "play-seed", "play-seed");
    el.seed.type = "text";
    el.seed.placeholder = "seed";
    el.seed.title = "a seed, to run this fight again exactly";
    el.start = make("button", "play-start", "play-start");
    el.start.textContent = "Play";
    el.setup.appendChild(el.seat);
    el.setup.appendChild(el.seed);
    el.setup.appendChild(el.start);

    el.round = make("div", "play-round", "play-round");
    el.order = make("div", "play-order", "play-order");
    el.budget = make("div", "play-budget", "play-budget");
    el.actions = make("div", "play-actions", "play-actions");
    el.hint = make("div", "play-hint", "play-hint");

    el.dice = make("div", null, "play-row play-dice");
    el.rollOwn = make("input", "play-roll-own", "play-roll-own");
    el.rollOwn.type = "checkbox";
    el.rollOwn.title = "roll the d20 yourself instead of letting the engine roll";
    el.rollLabel = make("label", null, "play-roll-label");
    el.rollLabel.textContent = "roll it yourself";
    /* The words are the checkbox's hit area as well as its name — a label
       beside a 13px box that does not answer a click is a target most people
       miss twice before reading it. */
    el.rollLabel.htmlFor = "play-roll-own";
    el.face = make("input", "play-face", "play-face");
    el.face.type = "text";
    el.face.placeholder = "face";
    el.face.title = "the face you rolled, or both faces with advantage: 17 or 17, 4";
    el.die = make("div", "play-die", "play-die");
    /* Nothing has been rolled yet, and the die says so by naming itself
       rather than by showing a number nobody threw. */
    el.die.dataset.faces = "idle";
    el.die.textContent = "d20";
    el.dice.appendChild(el.rollOwn);
    el.dice.appendChild(el.rollLabel);
    el.dice.appendChild(el.face);
    el.dice.appendChild(el.die);

    el.advance = make("button", "play-advance", "play-advance");
    el.advance.textContent = "End turn";

    [el.note, el.setup, el.round, el.order, el.budget, el.actions, el.hint,
      el.dice, el.advance].forEach(function (node) { root.appendChild(node); });

    el.seat.addEventListener("change", function () {
      seat = String(el.seat.value || "");
      armed = null;
      if (encounterId === null) { renderAll(); return; }
      refresh();
    });
    el.start.addEventListener("click", begin);
    el.advance.addEventListener("click", function () { endTurn(); });
    el.rollOwn.addEventListener("change", renderAll);
  }

  /* The cast, offered before the first round: the scene names everyone who
     will be in the fight, and a chair has to be takeable before there is an
     encounter to read it from. */
  function fillSeats(scene) {
    clear(el.seat);
    var whole = make("option");
    whole.value = "";
    whole.textContent = "the whole table";
    el.seat.appendChild(whole);
    (scene.combatants || []).forEach(function (spec) {
      var option = make("option");
      option.value = labelOf(spec);
      option.textContent = labelOf(spec);
      el.seat.appendChild(option);
    });
    el.seat.value = seat;
  }

  /* --- the contract ------------------------------------------------------ */
  /* The ten kinds — or however many this engine has — read off the request
     body of the route that takes them. Not a list in this file: a kind added
     to the engine reaches this bar without anybody editing this asset, and a
     kind removed from it stops being offered. */
  function loadKinds() {
    return ctx.request("GET", OPENAPI).then(function (response) {
      var document_ = response.json;
      if (response.status !== 200 || !document_ || !document_.paths) {
        kinds = [];
        kindsNote = "this launch's contract could not be read, so there is no "
          + "action list to offer";
        return;
      }
      var found = null;
      Object.keys(document_.paths).forEach(function (path) {
        var byMethod = document_.paths[path] || {};
        Object.keys(byMethod).forEach(function (method) {
          var operation = byMethod[method];
          if (!operation || operation.operationId !== ACT_OPERATION) { return; }
          var media = operation.requestBody && operation.requestBody.content
            && operation.requestBody.content["application/json"];
          var schema = media && media.schema;
          var kind = schema && schema.properties && schema.properties.kind;
          if (kind && Array.isArray(kind["enum"])) { found = kind["enum"].slice(); }
        });
      });
      if (found === null) {
        kinds = [];
        kindsNote = "this launch's contract declares no action kinds for "
          + ACT_OPERATION + ", so there is nothing to offer";
        return;
      }
      kinds = found;
      kindsNote = "";
    });
  }

  /* --- reading the fight -------------------------------------------------- */
  /* The one place that decides what this seat is allowed to hold, and it holds
     for writes as well as reads: whichever route the seat is named on, naming
     it is what makes the engine answer a brief instead of the whole fight.
     "" is the whole table's chair, which asks for nothing and is answered
     everything. */
  function seatQuery() {
    return seat === "" ? "" : "?as=" + encodeURIComponent(seat);
  }
  /* The whole table reads the authoritative state; a player reads their own
     brief, and never the other. */
  function readPath() {
    return seat === "" ? encounterPath() : encounterPath() + BRIEF + seatQuery();
  }
  function refresh() {
    return ctx.request("GET", readPath()).then(function (response) {
      if (response.status !== 200) {
        refusal(response, "the fight could not be read");
        return;
      }
      remember(response);
      snapshot = response.json;
      renderAll();
    });
  }

  function begin() {
    if (encounterId !== null) { return; }
    /* A shallow copy, because the seed is this panel's and the scene is the
       page's: Play writes nothing it was handed, and a seed written into
       ctx.scene would be a mutation of the editor's buffer by another name. */
    var body = {};
    Object.keys(ctx.scene).forEach(function (key) { body[key] = ctx.scene[key]; });
    var typed = String(el.seed.value || "").trim();
    if (typed !== "") { body.seed = Number(typed); }
    el.start.disabled = true;
    say("starting the fight…");
    return ctx.request("POST", ENCOUNTERS + seatQuery(), body).then(function (response) {
      if (response.status !== 201) {
        el.start.disabled = false;
        refusal(response, "the engine refused this scene");
        return;
      }
      remember(response);
      encounterId = String(response.json.encounter_id);
      el.note.textContent = "fight " + encounterId + " · seed "
        + response.json.seed;
      say("the fight is on");
      return refresh();
    });
  }

  /* --- what this seat may see -------------------------------------------- */
  /* The fight's cast, from whichever door it came through, and the one place in
     this file that knows there are two.

     encounter.state answers a flat `combatants` list. A brief answers `you`,
     `allies` and `enemies` instead — a shape that carries strictly more than
     the flat one, because an enemy entry comes with a `distance` and a `health`
     band in place of the sheet it is not sent. Nothing is stripped on the way
     through here: every entry is passed on as the engine wrote it, and the
     readers below take what their own row needs.

     `you` is the discriminator rather than `as`, because `you` is the field the
     difference is actually about: an answer that carries the asker's own entry
     as its own key is an answer that has been sorted into sides. */
  function cast() {
    if (!snapshot) { return []; }
    if (snapshot.you) {
      return [snapshot.you].concat(snapshot.allies || [], snapshot.enemies || []);
    }
    return snapshot.combatants || [];
  }
  /* Everyone this seat can see, in the order they will act.

     The GM's state names that order outright. A brief does not, and the
     omission is deliberate rather than an oversight: `order` would name every
     creature in the fight, the ambusher this seat has not detected included, so
     publishing it would undo the projection in one key. What a brief does carry
     is each visible creature's `initiative`, which is a number called out loud
     at a real table — so the rail this chair is entitled to is rebuilt from it,
     highest first, and a seat sees the running order of the fight it can see. */
  function railRows() {
    var everyone = cast();
    if (snapshot && snapshot.order) {
      return snapshot.order.map(function (name) {
        var found = { name: name };
        everyone.forEach(function (each) { if (each.name === name) { found = each; } });
        return found;
      });
    }
    return everyone.slice().sort(function (first, second) {
      return (second.initiative || 0) - (first.initiative || 0);
    });
  }
  function creatureAt(cell) {
    var found = null;
    cast().forEach(function (each) {
      var at = feetToCell(each.position || [0, 0]);
      if (at[0] === cell[0] && at[1] === cell[1]) { found = each; }
    });
    return found;
  }
  function fixtureAt(cell) {
    var features = (snapshot && snapshot.map && snapshot.map.features) || {};
    var found = null;
    Object.keys(features).forEach(function (name) {
      var square = features[name].square;
      if (Array.isArray(square) && square[0] === cell[0] && square[1] === cell[1]) {
        found = name;
      }
    });
    return found;
  }
  function actor() {
    var turn = snapshot && snapshot.turn;
    var found = null;
    cast().forEach(function (each) {
      if (each.name === turn) { found = each; }
    });
    return found;
  }
  /* Whether this chair may act right now. The whole table always may — it
     speaks for whoever's turn it is.

     A player is answered the question outright: `your_turn` is the brief's own
     verdict on it, so this reads the published answer rather than re-deriving
     one. Comparing `turn` to this seat's name gives the same verdict today, and
     it is worth knowing why it is the same rather than assuming it: a brief
     nulls `turn` when the creature acting is one this seat cannot see, and the
     asker is never one of those — so the two agree only because of a rule about
     a *different* field. Read the field that answers the question.

     The turn budget is the other half, because it is the acting creature's own
     and arrives only with a turn to spend it on. */
  function mayAct() {
    if (!snapshot || snapshot.over || !encounterId) { return false; }
    if (seat === "") { return true; }
    return !!snapshot.your_turn && !!snapshot.turn_state;
  }

  /* --- drawing ------------------------------------------------------------ */
  function drawTokens() {
    if (!snapshot) { ctx.setTokens(null); return; }
    ctx.setTokens(cast().map(function (each) {
      var token = {
        at: feetToCell(each.position || [0, 0]),
        team: each.team || "",
        label: String(each.name || "").slice(0, 2),
        down: each.hp === 0 || each.conscious === false,
        dead: !!each.dead,
        stable: !!each.stable
      };
      if (each.facing) { token.facing = each.facing; }
      /* Only where a number was actually sent. A brief's `enemies` carry a
         health band and no hit points at all — its `you` and its `allies` carry
         both, because a table shares its numbers — and a ring drawn from a band
         would be this page inventing the number the brief exists to withhold. */
      if (typeof each.hp === "number" && typeof each.max_hp === "number"
        && each.max_hp > 0) {
        token.hpFraction = each.hp / each.max_hp;
      }
      return token;
    }));
  }

  function renderRound() {
    el.round.classList.toggle("is-over", !!(snapshot && snapshot.over));
    if (!snapshot) { el.round.textContent = "no fight yet"; return; }
    if (snapshot.over) {
      el.round.textContent = "the fight is over" + (snapshot.winner
        ? " — " + snapshot.winner + " stands" : "");
      return;
    }
    el.round.textContent = "round " + snapshot.round + " · "
      + (snapshot.turn === null ? "someone you cannot see" : snapshot.turn)
      + "'s turn";
  }

  function renderOrder() {
    clear(el.order);
    if (!snapshot) { return; }
    railRows().forEach(function (creature) {
      var row = make("div", null, "play-seat-row");
      if (creature.name === snapshot.turn) { row.classList.add("play-current"); }
      var parts = [String(creature.name)];
      if (typeof creature.initiative === "number") {
        parts.push("init " + creature.initiative);
      }
      /* Whichever the seat was sent, and never both invented: the numbers
         where this chair has them — its own row and every ally's — and the
         band where it does not. The band's own vocabulary is the engine's
         (model.HEALTH_BANDS), so it is printed and never interpreted: a
         driver that ranked these words would be keeping a copy of a scale it
         is deliberately not told the edges of. */
      if (typeof creature.hp === "number" && typeof creature.max_hp === "number") {
        parts.push(creature.hp + "/" + creature.max_hp + " hp");
      } else if (creature.health) {
        parts.push(String(creature.health));
      }
      /* Only a brief measures one, and only for somebody other than you — the
         reach of every kind on the bar is decided by it, so a chair that has
         been told it should be able to read it without counting squares. */
      if (typeof creature.distance === "number") {
        parts.push(creature.distance + " ft away");
      }
      if ((creature.conditions || []).length) {
        parts.push(creature.conditions.join(", "));
      }
      row.textContent = parts.join(" · ");
      el.order.appendChild(row);
    });
  }

  /* One quantity, as the engine's own sentence and as the count beside it. The
     pips are the count and nothing else: a square of movement, an attack still
     in hand. There is no track behind them and no total to divide by, which is
     the same rule the health bands are read under — see the note on STYLE. */
  function gauge(text, count, unit) {
    var row = make("div", null, "play-gauge");
    row.dataset.state = count > 0 ? "held" : "spent";
    var label = make("span", null, "play-gauge-text");
    label.textContent = text;
    row.appendChild(label);
    var track = make("span", null, "play-gauge-track");
    for (var i = 0; i < count; i += 1) {
      track.appendChild(make("span", null, "play-pip play-pip-" + unit));
    }
    row.appendChild(track);
    return row;
  }
  /* One of the three that is either still yours or already spent. */
  function chip(text, spent) {
    var node = make("span", null, "play-chip");
    node.dataset.state = spent ? "spent" : "held";
    node.textContent = text;
    return node;
  }

  /* The turn budget. Each fact is its own element rather than a clause in one
     sentence, because "action spent" and "action available" differ by a word a
     reader has to find — and what a player needs mid-turn is to see, not read,
     what they have left. The words themselves are unchanged: an element adds a
     state beside the engine's own numbers, it does not restate them. */
  function renderBudget() {
    var budget = snapshot && snapshot.turn_state;
    clear(el.budget);
    el.budget.classList.toggle("is-resting", !budget);
    if (!budget) {
      el.budget.textContent = snapshot && seat !== ""
        ? "the turn budget is the acting creature's own"
        : "—";
      return;
    }
    el.budget.appendChild(gauge(
      budget.movement_left + " ft of movement left",
      Math.floor(budget.movement_left / FEET_PER_SQUARE), "move"));
    el.budget.appendChild(gauge(
      budget.attacks_left + " attacks left", budget.attacks_left, "attack"));
    var tokens = make("div", null, "play-chips");
    tokens.appendChild(chip(
      budget.action_used ? "action spent" : "action available",
      budget.action_used));
    tokens.appendChild(chip(
      budget.bonus_action_used ? "bonus action spent" : "bonus action available",
      budget.bonus_action_used));
    tokens.appendChild(chip(
      budget.interaction_used ? "interaction spent" : "interaction available",
      budget.interaction_used));
    /* A Loading weapon has had its one shot this turn. Shown as a spent chip
       like the rest, because that is what it is: a swing this creature still
       has the attacks for and cannot take with that weapon. */
    tokens.appendChild(chip(
      budget.loading_used ? "loading weapon fired" : "loading weapon ready",
      budget.loading_used));
    el.budget.appendChild(tokens);
  }

  /* Which button the battlefield is currently waiting on, marked without
     rebuilding the bar — the buttons carry click handlers, and replacing them
     to change a colour would be the look reaching into the wiring. */
  function markArmed() {
    var kind = armed ? armed.kind : null;
    Array.prototype.forEach.call(el.actions.children, function (button) {
      button.classList.toggle("is-armed", button.dataset.kind === kind);
    });
  }

  function renderActions() {
    clear(el.actions);
    if (kinds === null) { return; }
    var live = mayAct();
    kinds.forEach(function (kind) {
      var button = make("button", null, "play-action");
      button.dataset.kind = kind;
      /* Set only where there is one, so the stylesheet's `[data-wants]` reads
         as "this one will ask you to point at something" rather than matching
         every button that carries an empty string. */
      if (CLICK_FOR[kind]) { button.dataset.wants = CLICK_FOR[kind]; }
      button.textContent = kind.replace(/_/g, " ");
      button.disabled = !live;
      button.addEventListener("click", function () { choose(kind); });
      el.actions.appendChild(button);
    });
    markArmed();
    /* Ending a turn is taking one: a player's chair may end their own and
       nobody else's, and the whole table's may end whichever is running. */
    el.advance.disabled = !live;
  }

  function renderHint() {
    el.hint.classList.toggle("is-armed", !!armed);
    if (kinds !== null && kinds.length === 0) { el.hint.textContent = kindsNote; return; }
    if (!snapshot) { el.hint.textContent = "press Play to run this scene"; return; }
    if (snapshot.over) { el.hint.textContent = "nothing more to do here"; return; }
    if (armed) {
      el.hint.textContent = armed.wants === "creature"
        ? "click a creature to " + armed.kind.replace(/_/g, " ")
        : armed.wants === "fixture"
          ? "click the square of the fixture to work"
          : "click a square to move to";
      return;
    }
    if (!mayAct()) {
      el.hint.textContent = "it is " + (snapshot.turn || "someone you cannot see")
        + "'s turn, not yours";
      return;
    }
    el.hint.textContent = "choose an action";
  }

  function renderAll() {
    drawTokens();
    renderRound();
    renderOrder();
    renderBudget();
    renderActions();
    renderHint();
    el.face.disabled = !el.rollOwn.checked;
  }

  /* --- taking a turn ------------------------------------------------------ */
  function choose(kind) {
    var wants = CLICK_FOR[kind];
    if (!wants) { armed = null; markArmed(); post(kind, {}); return; }
    armed = { kind: kind, wants: wants };
    markArmed();
    renderHint();
  }

  function resolveClick(cell) {
    if (!armed) { return; }
    var pending = armed;
    if (pending.wants === "square") {
      armed = null;
      post(pending.kind, { to_position: cellToFeet(cell) });
      return;
    }
    if (pending.wants === "fixture") {
      var fixture = fixtureAt(cell);
      armed = null;
      post(pending.kind, fixture === null ? {} : { feature: fixture });
      return;
    }
    var creature = creatureAt(cell);
    if (creature === null) {
      say("no creature this seat can see is standing there", "error");
      return;
    }
    armed = null;
    var extra = { target: creature.name };
    var mine = actor();
    /* The actor's own options, read off the state the engine sent rather than
       named here — the same reason the kinds come from the contract. */
    if (pending.kind === "attack" && mine && (mine.attacks || []).length) {
      extra.attack = mine.attacks[0];
    }
    if (pending.kind === "cast" && mine && (mine.spells || []).length) {
      extra.spell = mine.spells[0];
    }
    if (pending.kind === "use_item" && mine) {
      var names = Object.keys(mine.items || {});
      if (names.length) { extra.item = names[0]; }
    }
    post(pending.kind, extra);
  }

  /* --- the dice ----------------------------------------------------------- */
  /* What the caller typed, exactly as typed. One number, or several for a roll
     made with two dice. Deliberately unchecked: a d20 face's range and how
     many a roll takes are the engine's rules, and a copy of them here would be
     a second opinion that has to be right — and would refuse a face the engine
     would have taken. What comes back instead is the engine's own sentence. */
  function typedFaces() {
    var written = String(el.face.value || "").trim();
    if (written === "") { return undefined; }
    var parts = written.split(",").map(function (part) { return Number(part.trim()); });
    return parts.length === 1 ? parts[0] : parts;
  }
  function showFace(value) {
    var pair = Array.isArray(value);
    /* Two faces are advantage, and both are shown. The attribute is how the
       type steps down to fit them — the panel is 240px wide and "17, 4" set at
       the size one face is set at does not fit on the printed face. */
    el.die.dataset.faces = pair ? "pair" : "one";
    el.die.textContent = pair ? value.join(", ") : String(value);
  }
  function randomFace() { return 1 + Math.floor(Math.random() * 20); }
  /* What the die is doing, said in the one channel a look can use. Presentation
     only: neither class decides a face, and both are set from the same two
     moments that write one — the frame the tumble starts on, and the frame it
     stops on, which is the frame the face is sent. */
  function dieState(state) {
    if (el === null) { return; }
    el.die.classList.toggle("is-rolling", state === "rolling");
    el.die.classList.toggle("is-settled", state === "settled");
  }

  /* The face the die stops on is the face that is sent — one variable, written
     once, read by the panel and by the request body. Nothing is posted while
     it is still turning, because until it stops there is no face to report. */
  function withFace(send) {
    if (!el.rollOwn.checked) { return send(undefined); }
    var typed = typedFaces();
    /* A face read off a die on the table was not thrown here, so nothing
       tumbles — but it is still the face that is about to be sent, and the die
       shows it lit for the same reason the thrown one does. */
    if (typed !== undefined) { showFace(typed); dieState("settled"); return send(typed); }
    var generation = tumbling + 1;
    tumbling = generation;
    var frame = window.requestAnimationFrame;
    var settle = function () {
      var face = randomFace();
      showFace(face);
      dieState("settled");
      send(face);
    };
    if (typeof frame !== "function") { settle(); return undefined; }
    dieState("rolling");
    var began = null;
    var step = function (now) {
      /* A driver that has been stopped, or a second roll that started while
         this one was turning: whichever it is, this die is no longer the one
         anybody is watching, so it settles nothing and sends nothing. */
      if (generation !== tumbling || el === null) { return; }
      if (began === null) { began = now; }
      if (now - began >= TUMBLE_MS) { settle(); return; }
      showFace(randomFace());
      window.requestAnimationFrame(step);
    };
    window.requestAnimationFrame(step);
    return undefined;
  }

  /* --- posting ------------------------------------------------------------ */
  function post(kind, extra) {
    if (encounterId === null) { return; }
    withFace(function (face) {
      var body = { kind: kind };
      Object.keys(extra).forEach(function (key) { body[key] = extra[key]; });
      /* Omitted rather than sent null when the engine is the one rolling: null
         is a value the dispatcher reads, and "you roll it" is the absence of a
         face rather than an empty one. */
      if (face !== undefined) { body.natural = face; }
      ctx.request("POST", encounterPath() + ACTIONS + seatQuery(), body, versionHeaders())
        .then(function (response) { settled(response, kind + " refused"); });
    });
  }

  function endTurn() {
    if (encounterId === null) { return; }
    withFace(function (face) {
      var body = {};
      if (face !== undefined) { body.natural = face; }
      ctx.request("POST", encounterPath() + ADVANCE + seatQuery(), body, versionHeaders())
        .then(function (response) { settled(response, "the turn could not be ended"); });
    });
  }

  /* The answer to a write, which named this chair in `as=` and so arrives
     already narrowed to it — its `state` in the brief's own shape, and its
     events with it, since Encounter.brief_events classifies an event's `data`
     key by key the way brief_of classifies a creature's fields. Only the events
     are read from it even so, and the fight re-read: what a seat holds is
     decided in one place, and the answer to a post is a fight one action old as
     soon as anybody else acts.

     `kind` is the field an event calls the thing that happened; this line read
     `each.type` for a release and rendered "2 events · undefined, undefined"
     every time anybody swung. It is also a field a player chair is served —
     EVENT_ENVELOPE_VISIBLE_KEYS names it — which `detail`, the GM's own
     sentence, deliberately is not, so there is nothing else here worth
     reaching for. */
  function settled(response, fallback) {
    if (response.status !== 200) {
      refusal(response, fallback);
      /* Re-read anyway: a refusal may be a stale version rather than an
         illegal action, and a panel showing the fight it thought it had is the
         thing that produced the stale write. */
      return refresh();
    }
    remember(response);
    var events = (response.json && response.json.events) || [];
    say(events.length
      ? events.length + " event" + (events.length === 1 ? "" : "s")
        + " · " + events.map(function (each) { return each.kind; }).join(", ")
      : "done");
    return refresh();
  }

  /* --- the seam ----------------------------------------------------------- */
  function start(context) {
    ctx = context;
    encounterId = null;
    version = null;
    snapshot = null;
    armed = null;
    kinds = null;
    kindsNote = "";
    injectStyle();
    clear(ctx.root);
    build(ctx.root);
    fillSeats(ctx.scene || {});
    onCanvasClick = function (event) {
      if (!armed) { return; }
      resolveClick(ctx.cellAt(event.clientX, event.clientY));
    };
    ctx.canvas.addEventListener("click", onCanvasClick);
    renderAll();
    return loadKinds().then(renderAll);
  }

  function stop() {
    tumbling += 1;
    if (ctx) {
      if (onCanvasClick) { ctx.canvas.removeEventListener("click", onCanvasClick); }
      ctx.setTokens(null);
      clear(ctx.root);
      /* The element goes back the way it was lent. The stylesheet stays — it
         is the page's for as long as the page lives, and every rule in it is
         scoped to a class that is now on nothing. */
      ctx.root.classList.remove("fivee-play");
    }
    onCanvasClick = null;
    el = null;
    ctx = null;
    snapshot = null;
    armed = null;
    encounterId = null;
    version = null;
  }

  return { start: start, stop: stop };
})();
