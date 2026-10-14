/*
 * Generator for XVS_Paystack_Live_Test_Book_v<VERSION>.docx (VERSION below)
 *
 * Run: NODE_PATH="$(npm root -g)" node gen_test_book.js
 *
 * The book is plain data (the SCENARIOS arrays and the PART blocks below) fed
 * to a few table helpers, so a correction is a text edit here and a rebuild.
 * No em dashes, and the word "branch" only, anywhere in the text.
 */
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell, Header, Footer,
  AlignmentType, LevelFormat, HeadingLevel, BorderStyle, WidthType, ShadingType,
  PageNumber, PageBreak, VerticalAlign, TabStopType,
} = require("docx");

const VERSION = "1.1";
const OUT = path.join(__dirname, `XVS_Paystack_Live_Test_Book_v${VERSION}.docx`);
const FONT = "Aptos";
const PAGE_W = 11906, PAGE_H = 16838, MARGIN = 1000;
const CONTENT_W = PAGE_W - 2 * MARGIN; // 9906

const C = {
  ink: "1F2937", dark: "1F2937", mid: "4B5563", light: "F3F4F6", line: "C7CCD4",
  live: "B45309", record: "0F766E", watch: "4B5563", accent: "1D4ED8", pale: "EEF2FF",
  warn: "FEF3C7", warnLine: "D97706",
};

/* ---------- inline text helpers ---------- */
let KEEP = false; // when true, paragraphs keep with the next so a scenario stays on one page where it fits

/** "plain **bold** plain" to TextRuns. */
function runs(text, o = {}) {
  const size = o.size || 19;
  const out = [];
  String(text).split(/(\*\*[^*]+\*\*)/g).forEach((part) => {
    if (!part) return;
    const bold = part.startsWith("**") && part.endsWith("**");
    out.push(new TextRun({
      text: bold ? part.slice(2, -2) : part, bold: bold || o.bold, italics: o.italics,
      color: o.color || C.ink, size, font: FONT,
    }));
  });
  return out;
}

const SPACERS = new Set();
function para(text, o = {}) {
  const p = new Paragraph({
    children: runs(text, o),
    spacing: { before: o.before ?? 0, after: o.after ?? 60, line: o.line || 252 },
    alignment: o.align, keepNext: o.keepNext ?? (KEEP ? true : undefined), keepLines: o.keepLines,
    indent: o.indent, border: o.border,
  });
  if (text === "") SPACERS.add(p);
  return p;
}

let stepInstance = 0;
function numbered(items, o = {}) {
  const instance = ++stepInstance;
  return items.map((t, i) => new Paragraph({
    children: runs(t, o),
    numbering: { reference: "steps", level: 0, instance },
    spacing: { before: 0, after: 40, line: 246 },
    keepLines: true, keepNext: KEEP ? true : undefined,
  }));
}
function bullets(items, o = {}) {
  return items.map((t) => new Paragraph({
    children: runs(t, o),
    numbering: { reference: "bul", level: 0 },
    spacing: { before: 0, after: 36, line: 246 },
    keepLines: o.keepLines ?? true, keepNext: KEEP ? true : undefined,
  }));
}

/* ---------- table helpers ---------- */

const thin = { style: BorderStyle.SINGLE, size: 4, color: C.line };
const BORDERS = { top: thin, bottom: thin, left: thin, right: thin };

function cell(children, width, o = {}) {
  return new TableCell({
    children: Array.isArray(children) ? children : [children],
    width: { size: width, type: WidthType.DXA },
    borders: BORDERS,
    shading: o.fill ? { fill: o.fill, type: ShadingType.CLEAR, color: "auto" } : undefined,
    margins: { top: 36, bottom: 36, left: 90, right: 90 },
    columnSpan: o.span, verticalAlign: o.valign || VerticalAlign.TOP,
  });
}
const label = (t, w, keepNext) => cell(para(t, { bold: true, size: 17, color: C.mid, keepNext, after: 0 }), w, { fill: C.light });

const GRID = [1500, 3450, 1500, 3456];
const BADGE = {
  LIVE: { text: "LIVE PAYSTACK", fill: C.live },
  RECORD: { text: "RECORD ONLY", fill: C.record },
  WATCH: { text: "WATCH ONLY", fill: C.watch },
};

/**
 * One scenario as one table.
 * s: { id, title, badge, branch, who, money, fees, note[], steps[], app[], paystack[], plain[], journal[], extra[] }
 */
const INDEX = [];
function scenario(s) {
  INDEX.push({ id: s.id, title: s.title, badge: s.badge });
  KEEP = true;
  const b = BADGE[s.badge];
  const rows = [];
  rows.push(new TableRow({
    cantSplit: true, tableHeader: false,
    children: [
      cell(para(`${s.id}   ${s.title}`, { bold: true, size: 22, color: "FFFFFF", keepNext: true, after: 0 }), GRID[0] + GRID[1] + GRID[2], { fill: C.dark, span: 3, valign: VerticalAlign.CENTER }),
      cell(para(b.text, { bold: true, size: 18, color: "FFFFFF", keepNext: true, after: 0, align: AlignmentType.CENTER }), GRID[3], { fill: b.fill, valign: VerticalAlign.CENTER }),
    ],
  }));
  const info = (a, av, c, cv) => new TableRow({
    cantSplit: true,
    children: [
      label(a, GRID[0], true), cell(para(av, { keepNext: true, after: 0, size: 18 }), GRID[1]),
      label(c, GRID[2], true), cell(para(cv, { keepNext: true, after: 0, size: 18 }), GRID[3]),
    ],
  });
  rows.push(info("Branch", s.branch, "Who does it", s.who));
  rows.push(info("Money needed", s.money, "Paystack fees", s.fees));
  const wide = GRID[1] + GRID[2] + GRID[3];
  const block = (name, kids, o = {}) => new TableRow({
    cantSplit: o.cantSplit ?? true,
    children: [label(name, GRID[0]), cell(kids, wide, { span: 3, fill: o.fill })],
  });
  if (s.note && s.note.length) {
    rows.push(block("Good to know", bullets(s.note, { size: 18 }), { fill: C.warn }));
  }
  rows.push(block("Steps", numbered(s.steps), { cantSplit: false }));
  rows.push(block("You should see in the app", bullets(s.app)));
  rows.push(block("You should see in Paystack", bullets(s.paystack)));
  const books = [...bullets(s.plain)];
  if (s.journal && s.journal.length) {
    books.push(para("Journal lines (Dr = debit, Cr = credit):", { bold: true, size: 17, color: C.mid, before: 40, after: 20, keepNext: true }));
    books.push(...s.journal.map((j) => new Paragraph({
      children: runs(j, { size: 17 }), spacing: { after: 24, line: 236 },
      indent: { left: 200 }, keepLines: true, keepNext: true,
    })));
  }
  rows.push(block("The books should show", books, { cantSplit: false }));
  KEEP = false;
  rows.push(new TableRow({
    cantSplit: true,
    children: [
      label("Result", GRID[0]),
      cell([
        para("[   ] Pass      [   ] Fail      [   ] Not run          Date: ____________      Initials: ________", { size: 18, after: 80 }),
        para("Notes: ______________________________________________________________________________", { size: 18, after: 30 }),
        para("_____________________________________________________________________________________", { size: 18, after: 0 }),
      ], wide, { span: 3 }),
    ],
  }));
  const table = new Table({ width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: GRID, rows });
  return [table, para("", { after: 100 })];
}

/** A plain table: header row plus body rows (arrays of strings). */
function simpleTable(widths, head, body, o = {}) {
  const total = widths.reduce((a, b) => a + b, 0);
  const mk = (t, w, isHead) => cell(
    Array.isArray(t) ? t : para(t, { bold: isHead, size: o.size || 18, color: isHead ? "FFFFFF" : C.ink, after: 0 }),
    w, { fill: isHead ? C.dark : undefined });
  const rows = [new TableRow({ tableHeader: true, cantSplit: true, children: head.map((h, i) => mk(h, widths[i], true)) })];
  body.forEach((r, ri) => rows.push(new TableRow({
    cantSplit: true,
    children: r.map((t, i) => {
      const k = cell(Array.isArray(t) ? t : para(t, { size: o.size || 18, after: 0, bold: o.boldFirst && i === 0 }), widths[i], {
        fill: ri % 2 === 1 && o.zebra ? "FAFAFB" : undefined });
      return k;
    }),
  })));
  return [new Table({ width: { size: total, type: WidthType.DXA }, columnWidths: widths, rows }), para("", { after: 120 })];
}

/** Checklist table: [ ] box, what to do, how to confirm. */
function checklist(items) {
  const W = [640, 4850, 4416];
  const rows = [new TableRow({ tableHeader: true, cantSplit: true, children: [
    cell(para("Tick", { bold: true, size: 17, color: "FFFFFF", after: 0 }), W[0], { fill: C.dark }),
    cell(para("What to do", { bold: true, size: 17, color: "FFFFFF", after: 0 }), W[1], { fill: C.dark }),
    cell(para("How you know it is right", { bold: true, size: 17, color: "FFFFFF", after: 0 }), W[2], { fill: C.dark }),
  ] })];
  items.forEach(([what, how]) => rows.push(new TableRow({ cantSplit: true, children: [
    cell(para("[   ]", { size: 18, after: 0 }), W[0]),
    cell(para(what, { size: 18, after: 0 }), W[1]),
    cell(para(how, { size: 18, after: 0 }), W[2]),
  ] })));
  return [new Table({ width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: W, rows }), para("", { after: 120 })];
}

function box(title, lines, fill = C.pale, edge = C.accent) {
  const kids = [para(title, { bold: true, size: 20, after: 60, keepNext: true })];
  lines.forEach((l) => kids.push(typeof l === "string" ? para(l, { size: 19, after: 50 }) : l));
  return [new Table({
    width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: [CONTENT_W],
    rows: [new TableRow({ cantSplit: false, children: [new TableCell({
      children: kids, width: { size: CONTENT_W, type: WidthType.DXA },
      shading: { fill, type: ShadingType.CLEAR, color: "auto" },
      margins: { top: 100, bottom: 100, left: 160, right: 160 },
      borders: { top: { style: BorderStyle.SINGLE, size: 4, color: edge }, bottom: { style: BorderStyle.SINGLE, size: 4, color: edge },
        left: { style: BorderStyle.SINGLE, size: 24, color: edge }, right: { style: BorderStyle.SINGLE, size: 4, color: edge } },
    })] })],
  }), para("", { after: 120 })];
}

const h1 = (t) => Object.assign(new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun({ text: t, font: FONT, bold: true, size: 34, color: C.ink })], spacing: { before: 120, after: 160 }, pageBreakBefore: true }), { __h1: true });
const h2 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun({ text: t, font: FONT, bold: true, size: 25, color: C.accent })], spacing: { before: 200, after: 100 }, keepNext: true });
const body = (t, o = {}) => para(t, { size: 20, after: 90, line: 264, ...o });

const content = [];
const add = (...x) => x.forEach((e) => {
  if (e && e.__h1) { while (content.length && SPACERS.has(content[content.length - 1])) content.pop(); }
  Array.isArray(e) ? content.push(...e) : content.push(e);
});

/* =====================================================================
 * COVER AND DOCUMENT CONTROL
 * ===================================================================== */
add(para("", { after: 1400 }));
add(new Table({
  width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: [CONTENT_W],
  rows: [new TableRow({ children: [new TableCell({
    width: { size: CONTENT_W, type: WidthType.DXA },
    shading: { fill: C.dark, type: ShadingType.CLEAR, color: "auto" },
    margins: { top: 500, bottom: 500, left: 400, right: 400 },
    borders: { top: { style: BorderStyle.NONE, size: 0, color: "FFFFFF" }, bottom: { style: BorderStyle.NONE, size: 0, color: "FFFFFF" }, left: { style: BorderStyle.NONE, size: 0, color: "FFFFFF" }, right: { style: BorderStyle.NONE, size: 0, color: "FFFFFF" } },
    children: [
      para("XVS FINANCE", { bold: true, size: 22, color: "9CA3AF", after: 160 }),
      para("Paystack Live Test Book", { bold: true, size: 64, color: "FFFFFF", after: 160, line: 300 }),
      para("Every money flow, tested once, with real money, on a live Paystack account", { size: 28, color: "E5E7EB", after: 0, line: 320 }),
    ],
  })] })],
}));
add(para("", { after: 400 }));
add(para("Test school: Codex Test School (live)", { size: 24, bold: true, after: 60 }));
add(para("Branches: Ikeja (main) and Lekki", { size: 22, after: 60 }));
add(para("Online money held by Codex (the default). Direct-to-bank tests start on 1 November 2026.", { size: 22, after: 60 }));
add(para("Amounts used: between ₦100 and ₦1,000, so a mistake costs little.", { size: 22, after: 600 }));
add(para(`Version ${VERSION}   |   14 October 2026   |   Owner's copy`, { size: 22, color: C.mid, after: 0 }));

add(h1("Document control"));
add(simpleTable([2300, 7606], ["Item", "Detail"], [
  ["Document", "XVS Paystack Live Test Book"],
  ["Version", VERSION],
  ["Date", "14 October 2026"],
  ["Written for", "The owner first (not an accountant). After that, the bursar-style roles who repeat the tests."],
  ["What it covers", "Every way money moves through Paystack in XVS finance, tested once with real money, plus the finance entries that never touch Paystack, so one book covers all of finance."],
  ["Built from", "Backend main d8e24f7a or later, and the builds carrying FinPro v0.7.69 or later. Screen names and buttons were read from that code on 14 October 2026."],
  ["Where a label is unsure", "The book describes the screen plainly (for example, the Branch Terminals tab) instead of guessing a button name. Part 9 lists what could not be confirmed."],
  ["Change log", "1.0: first issue (14 October 2026). 1.1 (14 October 2026): Codex top-ups are recorded and counted by the daily check (Block A, C1, new C7); payout and batch messages now say they are saved until approved; the write-off form shows the school's real bad-debts account; Block B asks for PAYSTACK_KEY_MODE and FinPro v0.7.69; new section 2.7, Rehearse on staging first."],
], { boldFirst: true }));

add(h2("Contents"));
add(simpleTable([900, 5400, 3606], ["Part", "What is in it", "Scenario IDs"], [
  ["1", "How to use this book: badges, how to read a scenario table, plain words, who you need", "-"],
  ["2", "Before you start: the run sheet, the money you need, one checklist for Paystack, the servers, the test school and the test data, and a staging rehearsal", "-"],
  ["3", "Money in (LIVE PAYSTACK): pay links, desk QR, pupil accounts, branch terminals, part payments, credit, failed payments, the return page", "M1 to M12"],
  ["4", "Money back: refunds through Paystack, refunds from the bank, disputes", "R1 to R5"],
  ["5", "Money out: vendor payouts, payroll through Paystack, daily settlement, Codex's own payouts", "O1 to O14"],
  ["6", "Checks and controls: daily balance check, who may approve, branch walls, limits", "C1 to C7"],
  ["7", "RECORD ONLY finance transactions: nothing goes to Paystack", "B1 to B14"],
  ["8", "From 1 November 2026: direct custody (money straight to each branch's bank)", "D1 to D6"],
  ["9", "Known limits, things Paystack has not confirmed, and the results summary", "All"],
]));

/* =====================================================================
 * PART 1
 * ===================================================================== */
add(h1("Part 1. How to use this book"));
add(body("This book walks you through every way money moves in XVS finance. You do each test once, tick Pass or Fail, and write down what you saw. You do not need to understand accounting. Where an accounting word is unavoidable, a plain word sits beside it."));
add(h2("The three badges"));
add(simpleTable([2200, 7706], ["Badge", "What it means"], [
  ["LIVE PAYSTACK", "Real money moves through your live Paystack account. A card is charged, a bank transfer is made, or Paystack sends money out. Read the money line first."],
  ["RECORD ONLY", "Nothing goes to Paystack. You only write something down in the app (a receipt, a credit note, a journal). Money may not move at all."],
  ["WATCH ONLY", "Cannot be forced to happen (a dispute, a failed transfer). The table says what to do and what to check if it ever does. Leave Not run unless it happens."],
]));
add(h2("How to read one scenario table"));
add(simpleTable([2300, 7606], ["Row", "What it tells you"], [
  ["Header band", "The ID (M1, R2, O5 and so on), the title, and the badge."],
  ["Branch, Who does it", "Which branch the money belongs to, and who sits at the keyboard."],
  ["Money needed, Paystack fees", "How much of your own money to have ready, and the fee to expect. Fees are \"about\" figures: confirm them on paystack.com/pricing."],
  ["Good to know", "A yellow row, only when there is a trap. Read it before you start."],
  ["Steps", "Short numbered steps. Menu paths are written as: Payments, Money In, Collections."],
  ["You should see in the app", "What the screens must show when it works."],
  ["You should see in Paystack", "What your Paystack dashboard must show. Dashboard page names may differ slightly from the names used here."],
  ["The books should show", "First in plain words, then the journal lines. A journal line is one side of an accounting entry: Dr is debit, Cr is credit. You only compare account names and amounts."],
  ["Result", "Tick Pass or Fail, add the date and your initials, and write anything odd under Notes."],
]));
add(h2("Plain words used in this book"));
add(simpleTable([2900, 7006], ["Word", "What it means here"], [
  ["Held money, held balance", "In the default setup, parents' online payments go into Codex's Paystack balance. Codex keeps a running figure of what it holds for each branch. A branch can spend only its own held money."],
  ["Gateway clearing (1125)", "Online payments the books already know about but that have not yet reached the branch's bank. It should return to nothing once everything is settled."],
  ["Customer credit (2140)", "Money a parent paid that is not tied to a bill. It can later be applied to a bill or refunded."],
  ["Unidentified receipts (2195)", "Money paid at a branch terminal that nobody has yet matched to a parent."],
  ["Settlement", "Codex sending the money it holds for a branch to that branch's own bank account. Two Codex people must approve each one."],
  ["Withholding tax (WHT)", "A small tax the school keeps back from a supplier's payment and pays to the tax office later. The supplier receives the amount minus the tax."],
  ["Subaccount", "A Paystack account for one branch's bank details. In the default setup it is only used so Codex knows the branch's bank code."],
  ["Preparer, approver", "The person who prepares a payment is never allowed to approve it. A second real person must."],
  ["Codex, platform", "Codex Techhub, who runs XVS. The school is Codex's customer. Some steps are done by Codex's own staff in the Codex console."],
]));
add(h2("Who you need"));
add(body("You can play several parts yourself, but an approval always needs a second real person with their own sign-in. The app refuses to let the person who prepared something approve it."));
add(simpleTable([2700, 7206], ["Role", "What they do in this book"], [
  ["You (the owner)", "Plays parent (pays with your own cards and bank accounts), vendor, and staff member A. Also prepares most items."],
  ["Ikeja bursar", "A school user who works only in Ikeja. Used for the branch-wall checks."],
  ["Lekki bursar", "A school user who works only in Lekki."],
  ["School administrator", "Covers the whole school. Changes settings, creates bank accounts, edits the approver groups."],
  ["Second person (approver)", "A real second person. Approves refunds, payouts, payroll and expense claims, and confirms a staff bank account that did not match."],
  ["Codex finance operator", "Codex staff. Puts settlements forward, and raises Codex's own payouts."],
  ["Two Codex approvers", "Two distinct real Codex people. Both must approve every settlement."],
]));
add(box("Four rules for a tired tester", [
  "1. One scenario at a time. Write the payment reference (it starts with CXP-) in Notes whenever a scenario creates one.",
  "2. If something reads Pending, press Re-verify or Check with Paystack first. Give it a few minutes before you call it a fail.",
  "3. Never edit, delete or void something to make a scenario pass. Write down what happened and carry on.",
  "4. If money moved and the app shows nothing, stop and tell the developer before doing more tests.",
]));

/* =====================================================================
 * PART 2
 * ===================================================================== */
add(h1("Part 2. Before you start"));
add(body("Do this part once. It covers the order of the tests, the money you need, the Paystack account, the servers, the test school with its test data, and a rehearsal on staging. Tick every box. Several tests will fail in confusing ways if one box is missed."));

add(h2("2.1 The order of the tests (run sheet)"));
add(body("Some tests need the money or the credit left by an earlier one, and a few must wait for the next morning. Follow this order."));
add(simpleTable([1300, 2400, 6206], ["When", "Scenarios", "Why this order"], [
  ["Day 0", "Part 2 (all of it), then C7 steps 1 to 6", "Set-up. Nothing is paid by parents yet. The top-up is added and recorded now so the daily check can agree from the first day."],
  ["Day 1, first", "M1, M2, M3, M4, M5, M6, M7, M8, M10, M11, M12", "These put money into Ikeja's and Lekki's held balances. M10 is the only Lekki payment so far, on purpose: Lekki stays short for O7."],
  ["Day 1, then", "R1, R3, R4", "Refunds need the credit left by M6 (Emeka) and M5 (Dayo) and need held money to draw on."],
  ["Day 1, then", "B11, then O1, O2", "B11 records the vendor bills the payouts settle. Vendor payouts then draw on Ikeja's held money."],
  ["Day 1, then", "O3, O4, O5, O6, O7", "Payroll. In O7 Lekki's line waits for funds because Lekki holds too little."],
  ["Day 1, then", "M9, then O8, then R2", "M9 is a larger Lekki payment. It releases the waiting payroll line (O8). R2 refunds part of M9 afterwards."],
  ["Day 1, then", "O9, O10, C3, C6", "Staff with no bank details, and the control checks that need no overnight wait."],
  ["Day 2, morning", "O12, then C1 (and C7 step 7)", "The settlement is built at about 06:30 and claims everything paid before today. The balance check runs at about 07:15."],
  ["Day 2", "O13, O14, C5", "Codex's own payouts and the over-limit payout, once the top-up is booked."],
  ["Any day", "B1 to B13", "RECORD ONLY. B14 (closing a month) goes last, because it blocks that month."],
  ["Next month", "O11 with C2", "Payroll from the school's bank needs a pay date in a month not yet paid. C2 uses the same draft run."],
  ["1 Nov 2026 on", "D1 to D6, then C4", "Direct custody. The switch can only take effect on the 1st of a month."],
]));
add(body("The held-money settlement runs once a day. A payment made today is first settled tomorrow morning. Times are server times (the schedule is 06:30 and 07:15), and the day cut-off uses the school's own day.", { size: 18 }));

add(h2("2.2 The money you need"));
add(body("Every payout comes back to you, so the real cost is mostly Paystack's fees. Amounts are rounded; net pay in the payroll rows is a little less than gross."));
add(simpleTable([3600, 1700, 4606], ["Item", "Amount", "Where it goes"], [
  ["Parent payments, Ikeja: M1 to M8", "₦3,350", "M1 ₦500, M2 ₦400, M3 ₦300, M4 ₦250, M5 ₦700, M6 ₦500, M7 ₦350, M8 ₦350"],
  ["Parent payments, Lekki: M10 and M9", "₦1,300", "M10 ₦300, M9 ₦1,000"],
  ["Total you put in on Day 1", "₦4,650", "From your cards and bank accounts"],
  ["Refunds back to you", "₦800", "R1 ₦500, R2 ₦200, R4 ₦100"],
  ["Vendor payouts", "₦1,450 sent + ₦20 fees", "O1 ₦500, O2 ₦950 (₦1,000 less ₦50 tax)"],
  ["Payroll through Paystack", "Under ₦1,100 + ₦20 fees", "Staff A (gross ₦500) and Staff B (gross ₦600), about ₦10 fee each"],
  ["Paystack balance top-up (you add it)", "₦3,500", "See 2.3 and C7. Record it so the daily check counts it. It is also what Codex's own payouts O13 and O14 draw on"],
  ["From 1 November (D2 to D4)", "₦1,000", "₦400 card, ₦300 dedicated account, ₦300 terminal"],
  ["Fees for the whole book", "about ₦150 plus unknowns", "Card payments about 1.5% each. Add Paystack's charges for bank-transfer checkout, dedicated accounts and terminals: check your Paystack pricing page."],
]));
add(box("Paystack fees, as best the book knows (confirm on paystack.com/pricing)", [
  "Local cards: about 1.5%. For payments of ₦2,500 and over Paystack adds ₦100, and the fee is capped at ₦2,000. Every payment in this book is under ₦2,500, so only the 1.5% applies.",
  "Transfers out: about ₦10 up to ₦5,000, about ₦25 from ₦5,001 to ₦50,000, about ₦50 above that. This is the same table the app uses (app code checked). The app does not read the fee back from Paystack.",
  "Bank-transfer checkout, dedicated accounts and terminals: check your Paystack pricing page. The book does not guess.",
  "Refunds: Paystack does not give back the fee it kept on the original payment, and the app books nothing for it.",
], C.warn, C.warnLine));

add(h2("2.3 Block A. The Paystack account"));
add(checklist([
  ["Your Paystack business is in live mode and allowed to send transfers. Check the Transfers page opens.", "You can see Transfers, Refunds and Balance in the dashboard."],
  ["The live secret key and live public key are in the production settings (PAYSTACK_SECRET_KEY and PAYSTACK_PUBLIC_KEY), PAYSTACK_KEY_MODE is live, and the web service and worker were restarted. No test key anywhere.", "The developer confirms. Keys start sk_live_ and pk_live_. A server refuses to start if the mode is missing or a key is the other kind."],
  ["Live webhook URL set in the Paystack dashboard: https://api.codexng.com/v1/payments/webhooks/paystack/", "The dashboard shows it under live settings. Paystack signs each call with the secret key, so the key above must be the one in use."],
  ["Callback URL in the dashboard set to https://pay.xvs.codexng.com/payments/return (see the note below).", "The dashboard shows it. Each school checkout carries its own return address anyway."],
  ["Transfer OTP (the one-time code Paystack asks for on a transfer) is turned off in the dashboard.", "With it on, every payout sits as Processing forever, because nobody is there to type the code."],
  ["Paystack is not set to pay your balance out to your bank on its own, and the Codex platform setting \"Paystack balance swept automatically\" is Off (Payments, Platform Controls, Held Reconciliations).", "The setting shows Off, \"Default (off)\" or \"Platform setting\" with Off."],
  ["Dedicated virtual accounts are switched on for your Paystack business.", "Paystack confirms. Without this, M5 cannot start."],
  ["The old hand-made Virtual Terminal in the Paystack dashboard is deactivated.", "Its payments would arrive at the app as a terminal the app does not own, so they could not be placed with a branch."],
  ["Balance funded: add ₦3,500 to your Paystack balance from a Codex bank account (see 2.2). Codex needs that bank account entered in its own books.", "The dashboard balance shows it."],
  ["Record the top-up in the Codex console: Payments, Platform Controls, Paystack Top-ups, New top-up. Enter Amount, Paid from (one of Codex's own bank accounts), Date the money left the bank (not in the future), a Reference and a Note, then Save top-up and Submit for approval. Two Codex approvers from the held settlement approver group approve it, never the person who recorded it. This is scenario C7.", "The top-up reads as booked. The next daily check counts it and agrees with Paystack."],
  ["If Paystack already holds money that belongs to a branch from earlier payments, a Codex operator records it once per branch before you start (the command record_held_opening_balance).", "For a brand-new school there is nothing to record. The daily check (C1) will show any other money in Paystack that is not in the books as a difference."],
]));
add(box("Two notes on Block A", [
  "**Callback URL.** In the code, a school's checkout returns the parent to the school's own address, for example https://<school>.xvs.codexng.com/payments/return. The pay.xvs.codexng.com address is used only for invoices Codex itself raises, and as a fallback. Setting it in the dashboard does no harm. Expect the school address in M1.",
  "**Top-up and the daily check.** Until a top-up is recorded and approved, Paystack holds more than Codex's books explain, so the daily check (C1) disagrees by about the top-up. Once it is booked the check counts it and agrees. Any other money already in Paystack that is not in the books still shows as a difference. Write it here: ₦____________.",
]));

add(h2("2.4 Block B. The servers (the developer does this, you tick it)"));
add(checklist([
  ["Database migrations have run on production.", "The developer confirms."],
  ["Deploy commands have run, in this order: seed_payments_permissions, seed_finance_permissions, sync_field_registry, seed_finance_approvals --all-tenants, backfill_virtual_terminals.", "Each reports Done. seed_finance_approvals gives every school its payroll approval route and its refund and write-off approval routes."],
  ["Terminals are on: PAYMENTS_TERMINALS_AUTO_CREATE is true in production (the code's setting name; it is true unless someone turned it off).", "Creating a branch opens its terminal at Paystack by itself."],
  ["The worker and the scheduler are running (they send payouts, settle, re-check payments every 15 minutes and run the daily check).", "The developer confirms the worker log shows scheduled tasks."],
  ["The school app and the Codex console builds deployed carry FinPro v0.7.69 or later, and the backend is main d8e24f7a or later. That brings the Paystack Top-ups screen, the saved-until-approved payout messages and the return page that shows live payment status.", "The Payments menu shows Branch Terminals and Terminal Payments, and Payroll shows a Bank accounts tab."],
  ["Every deployed service states PAYSTACK_KEY_MODE (live on production, test on staging) and holds Paystack keys of that kind. Staging uses test keys and the test-mode webhook https://api-staging.codexng.com/v1/payments/webhooks/paystack/ in the Paystack Test tab.", "A service with the wrong kind of key will not start. This protects staging from charging real cards."],
  ["If creating a payout ever says no approval route was found (TEMPLATE_NOT_FOUND), the developer runs seed_payout_approvals for that school.", "A new school gets its payout route when it is created, so this is only for older ones such as Codex's own books."],
]));

add(h2("2.5 Block C. The test school"));
add(checklist([
  ["In the Codex console (intranet.codexng.com) create the school \"Codex Test School\" on live. Its address is <slug>.xvs.codexng.com. Sign in as its administrator.", "You can open the address and sign in."],
  ["Give the school two branches: Ikeja (the main branch) and Lekki. A school always has a main branch.", "The branch chooser in the app lists both."],
  ["Give each branch its own phone number that receives WhatsApp (the school's Branches screen). The main branch takes the school's phone when the school is created.", "Payments, Money In, Branch Terminals shows two cards, each reading Open. A card reading \"Waiting for a phone number\" has no number yet: add one on the card."],
  ["Who holds online payments: Codex (held). In Finance Settings, Banking and cash, the Online payments panel shows \"Who holds online payments\" as held, with Settlement interval (days) 1.", "This is the default. Do not change it until Part 8."],
  ["Create one bank account per branch (Operations, Banking, Bank Accounts, New bank account): your two real accounts. Link each to a ledger cash account, set its branch, and tick the collection box (in the new-account form it reads \"Print on invoices/receipts\"; on the saved account it reads Collection). Ikeja gets account X, Lekki gets account Y.", "Each account shows the Collection badge."],
  ["For each branch, in the Online payments panel select Create subaccount, enter the bank code at Paystack (for example 058 for GTBank, 057 for Zenith) and select save.", "Each branch reads \"Set up with Paystack\". Without this, settlements (O12) cannot be paid."],
  ["Approver groups (Workflow, Approvers, Groups). Put your second real person in: payout-approver (payouts), the adjustment approver group (refunds, write-offs, credit notes), the expense claim approver group, and the payroll approver group. Give that person a role that can post payroll.", "Each group shows at least one effective approver. A group nobody is in makes the request wait."],
  ["Codex side: the group held-settlement-approver holds two distinct real Codex people. Codex has a finance operator who can see Payments, Platform Controls, Held Settlements.", "The Codex console shows Held Settlements."],
  ["Check the school's roles hold the keys these tests use (Settings, Roles). The seed gives the Payments keys only to the Codex admin roles, so a school role may lack some; add any that are missing: payments.collection view and create, payments.virtual_account view and create, payments.refund view and create, payments.payout view and create, payments.terminal view and update, payments.terminal_payment view, match and reverse, payments.report view, payments.settings view and update, payments.settlement create, finance.staff_bank view, update and confirm, and the payroll keys view, create, submit, post and pay. Procurement vendor access (including vendor governance, for KYC) goes to whoever sets up the vendor.", "Each tester sees the menu items their steps name. A missing key shows as a screen saying you have no access."],
  ["Create the withholding tax code for O2: Ledger & Setup, Tax & Currency, Tax Codes, New tax code. Code WHT-5, Treatment Standard rated, Rate 5, Name \"Withholding tax 5%\", Collected a/c: WHT payable (2300).", "The code appears in the list. Ask your accountant if the school already has one."],
]));

add(h2("2.6 Block D. Test people and test data"));
add(body("Use plus-addresses (you+ada@gmail.com) if your mail supports them: each is a different address to the app but lands in your inbox. Every customer needs a unique billing email, and the terminal tests match on it."));
add(simpleTable([1500, 1500, 3000, 3906], ["Customer", "Branch", "Billing email", "Used in"], [
  ["Ada", "Ikeja", "you+ada@...", "M1 (₦500 bill), M2 (₦400 bill)"],
  ["Bola", "Ikeja", "you+bola@...", "M3 (₦300 bill), M4 (₦600 bill), M8"],
  ["Dayo", "Ikeja", "you+dayo@...", "M5 (₦450 bill, virtual account), R3, R4"],
  ["Emeka", "Ikeja", "you+emeka@...", "M6, R1"],
  ["Gbenga", "Ikeja", "you+gbenga@...", "M7 (₦350 bill), M11 (₦100 bill)"],
  ["Chidi", "Lekki", "you+chidi@...", "M9, R2"],
  ["Funke", "Lekki", "you+funke@...", "M10 (₦300 bill)"],
], { boldFirst: true }));
add(body("Create each with Receivables, Customers & Billing, Customers / Payers, New customer (name, billing email, billing phone, branch). Raise each bill just before its scenario: Invoices, New invoice, customer, one line with an income account such as 4100 Operating Revenue and the amount, keep \"Issue now (post it to the books)\" ticked, then Issue invoice."));
add(simpleTable([1500, 1500, 1300, 5606], ["Staff", "Branch", "Gross", "Notes"], [
  ["Staff A", "Ikeja", "₦500", "You, under your real name, with a school sign-in. Your bank account X holds your name, so it will match."],
  ["Staff B", "Lekki", "₦600", "A test person, for example \"Tola Lekki\", with a sign-in you control. You will give this person your account Y, whose holder name is yours, so it will NOT match and needs a second person."],
  ["Staff C", "Ikeja", "₦300", "A test person with a sign-in and no bank account saved. Used for the Needs bank details line."],
  ["Vendor", "Ikeja", "-", "\"Test Vendor Ltd\", with your account X or Y as its bank details (O1, O2, B11)."],
], { boldFirst: true }));
add(body("Each staff member needs a school sign-in so a bank account can be saved for them. Add them in the school's staff area, then in Payroll, Employee salaries, Add employee: choose the Staff member, the Branch and the Gross, with Salary structure \"Flat\".", { size: 18 }));

add(h2("2.7 Rehearse on staging first"));
add(body("Before spending real money, run the whole book once on staging with Paystack's test cards. Nothing real moves, so mistakes cost nothing and you learn the screens."));
add(bullets([
  "Staging uses Paystack test keys (PAYSTACK_KEY_MODE test) and the test-mode webhook https://api-staging.codexng.com/v1/payments/webhooks/paystack/ set in the Paystack Test tab. A staging server will not start with a live key.",
  "Create a staging test school the same way (Block C) and use Paystack's published test cards (see Paystack's test-payments page) for every card payment.",
  "Run the same scenarios in the same order. Mark the rehearsal in the Notes boxes, or print a second copy of the results table.",
  "A rehearsal cannot show real bank transfers, real WhatsApp alerts, real fees or real settlements. Test mode may also behave differently for dedicated accounts, terminals and transfers; the book has not confirmed which. Dedicated accounts use the test bank on a test key.",
  "Do not move on to live until the rehearsal results are all Pass, or each Fail is explained.",
], { size: 19 }));

/* =====================================================================
 * PART 3. MONEY IN
 * ===================================================================== */
add(h1("Part 3. Money in (LIVE PAYSTACK)"));
add(body("Every payment here is real. Parents' online money lands in Codex's Paystack balance, and Codex's books record what it holds for each branch. Remember the order in 2.1: M10 comes before the payroll tests, and M9 comes after O7."));
add(box("The same two journal lines appear in almost every payment test", [
  "When Paystack confirms a payment tied to a bill: **Dr 1125 Gateway clearing, Cr 1200 Accounts receivable**, in the branch of the bill. The parent no longer owes it, and the money is \"online payments not yet in the bank\" until the settlement (O12).",
  "At the same moment Codex's own books (branch Lagos) record **Dr 1127 Payment Provider Balance, Cr 2180 Client Funds Held**, for the payment less Paystack's fee. That is the held balance for the branch. The school app shows it in Finance Settings, Banking and cash, Online payments.",
]));

add(scenario({
  id: "M1", title: "Pay link, card payment", badge: "LIVE", branch: "Ikeja",
  who: "You as bursar (make the link), then you as parent (pay it)",
  money: "₦500 of your own, on a card", fees: "About 1.5% of ₦500, about ₦7.50, kept by Paystack",
  steps: [
    "Raise the bill: Receivables, Customers & Billing, Invoices, New invoice. Customer Ada, one line of ₦500, keep Issue now ticked, select Issue invoice.",
    "Open the invoice, go to the Settlements tab, select Request payment, then Generate payment link. The amount is the whole balance, ₦500.",
    "Select Copy. Paste the link into a private browser window and pay with your card.",
    "Wait for Paystack to send you back, then in the app open Payments, Money In, Collections.",
  ],
  app: [
    "The return page says **Payment confirmed**, shows ₦500.00 and \"paid to Codex Test School\", and quotes the payment reference.",
    "Collections: the row reads Paid. Open it to see the status timeline and the receipt it booked.",
    "The invoice reads Paid and its balance is ₦0.",
    "Finance Settings, Banking and cash, Online payments: Ikeja's held amount went up by ₦500 less the fee.",
  ],
  paystack: [
    "Transactions: a successful ₦500 payment whose reference starts CXP-, the same reference the app shows.",
    "The fee on the transaction is the fee the app keeps against the payment.",
  ],
  plain: [
    "The parent no longer owes ₦500. The money sits as online payments not yet in the bank until the settlement.",
    "Codex's books hold ₦500 less the fee for Ikeja.",
  ],
  journal: [
    "Codex Test School, Ikeja: Dr 1125 Gateway clearing ₦500 / Cr 1200 Accounts receivable ₦500.",
    "Codex's books, Lagos: Dr 1127 Payment Provider Balance / Cr 2180 Client Funds Held, ₦500 less the fee.",
  ],
}));

add(scenario({
  id: "M2", title: "Pay link, bank transfer on the checkout", badge: "LIVE", branch: "Ikeja",
  who: "You as bursar, then you as parent",
  money: "₦400 sent from your bank app", fees: "Check your Paystack pricing page for pay-with-transfer",
  note: ["Send exactly ₦400 within the time Paystack shows. If you send another amount, do not try to fix it with a second transfer: note what Paystack and the app do."],
  steps: [
    "Raise a ₦400 bill for Ada and issue it (as in M1).",
    "On the invoice, Settlements tab, Request payment, Generate payment link, Copy.",
    "Open the link in a private window. On Paystack's page choose the bank-transfer option. Paystack shows a one-time account number.",
    "In your bank app send ₦400 to that number. Wait for the return page, then check Collections.",
  ],
  app: [
    "Collections: the row reads Paid after Paystack confirms. If it still reads Pending, open the row and select Re-verify.",
    "The invoice reads Paid. The return page says Payment confirmed.",
  ],
  paystack: ["Transactions: a successful ₦400 payment through bank transfer, with the CXP- reference."],
  plain: ["Same as M1 with ₦400. A payment by transfer is booked exactly like a card payment."],
  journal: ["Ikeja: Dr 1125 Gateway clearing ₦400 / Cr 1200 Accounts receivable ₦400.", "Codex's books: held for Ikeja up by ₦400 less the fee."],
}));

add(scenario({
  id: "M3", title: "Desk QR: Collect at the desk", badge: "LIVE", branch: "Ikeja",
  who: "You as Ikeja bursar, then you as parent on a second phone",
  money: "₦300 on a card", fees: "About 1.5%, about ₦4.50",
  steps: [
    "Raise a ₦300 bill for Bola and issue it.",
    "Open the invoice, Settlements tab, select Collect at the desk. The amount is ₦300 and cannot go above the balance. Leave the payer's email blank (the family's billing email is used).",
    "Select Show payment code. A large QR code appears.",
    "Scan the code with your phone and pay by card. Watch the banner on the bursar's screen.",
  ],
  app: [
    "The banner reads **Waiting for payment**, then **Paid** (\"The payment has been received and the receipt is booked\").",
    "Collections: the row reads Paid and its drawer says \"Taken at the desk\". The sources filter set to Desk lists it.",
    "The parent lands on the school's own address, never on the staff console.",
    "If you wait ten minutes without paying, the banner changes to \"Not paid yet\" and offers Check again.",
  ],
  paystack: ["Transactions: a successful ₦300 payment with the CXP- reference."],
  plain: ["Same as M1 with ₦300. The desk only changes how the payer gets the link."],
  journal: ["Ikeja: Dr 1125 Gateway clearing ₦300 / Cr 1200 Accounts receivable ₦300."],
}));

add(scenario({
  id: "M4", title: "Part payment at the desk", badge: "LIVE", branch: "Ikeja",
  who: "You as Ikeja bursar, then you as parent", money: "₦250 on a card", fees: "About 1.5%, about ₦3.75",
  note: ["Request payment (M1) always asks for the whole balance. Only Collect at the desk lets you choose a smaller amount. Leave the ₦350 that remains open: M8 uses it."],
  steps: [
    "Raise a ₦600 bill for Bola and issue it.",
    "Open the invoice, Settlements tab, Collect at the desk. Change the Amount to ₦250.",
    "Show payment code, scan it, pay.",
  ],
  app: [
    "The banner reads Paid. The invoice reads **Partly paid** with a balance of ₦350.",
    "The invoice's Settlements tab lists one ₦250 payment.",
  ],
  paystack: ["Transactions: a successful ₦250 payment."],
  plain: ["The parent still owes ₦350 on that bill. Only ₦250 moved."],
  journal: ["Ikeja: Dr 1125 Gateway clearing ₦250 / Cr 1200 Accounts receivable ₦250."],
}));

add(scenario({
  id: "M5", title: "Pupil's own bank account (dedicated virtual account), with an overpayment", badge: "LIVE", branch: "Ikeja",
  who: "You as bursar, then you as parent in your bank app",
  money: "₦700 sent from your bank app (₦250 more than the bill, on purpose)", fees: "Check your Paystack pricing page for dedicated accounts",
  note: [
    "A transfer into a pupil's account is never applied to a bill by itself. It waits as customer credit until a bursar allocates it (step 5).",
    "The ₦250 left over is Dayo's credit, used by R3 and R4. Do not use it for anything else.",
  ],
  steps: [
    "Raise a ₦450 bill for Dayo and issue it.",
    "Payments, Money In, Virtual Accounts, New virtual account. Customer Dayo, provider Paystack, leave the bank blank, select Provision account.",
    "Open the new account and note its number and bank. The number is shown only to people allowed to see it.",
    "From your bank app send ₦700 to that number.",
    "When Collections shows the transfer as Paid, go to Receivables, Money Received, Receipts & Allocation, the Unallocated tab, open Dayo's receipt, keep Auto-allocate on (oldest first) and apply.",
  ],
  app: [
    "Virtual Accounts: Dayo's account reads Active.",
    "Collections: a row marked **Virtual account transfer**, Paid, ₦700.",
    "Before step 5 the receipt is Unallocated ₦700. After step 5 the bill reads Paid and ₦250 remains as Dayo's credit.",
  ],
  paystack: [
    "Customers: Dayo has a dedicated account. Transactions: a successful ₦700 payment through that account.",
  ],
  plain: [
    "The ₦700 is real and sits in online payments not yet in the bank. ₦450 pays the bill. ₦250 is a credit the school owes Dayo's family.",
  ],
  journal: [
    "On arrival, Ikeja: Dr 1125 Gateway clearing ₦700 / Cr 2140 Customer Credit ₦700.",
    "On allocating: Dr 2140 Customer Credit ₦450 / Cr 1200 Accounts receivable ₦450.",
    "Codex's books: held for Ikeja up by ₦700 less the fee.",
  ],
}));

add(scenario({
  id: "M6", title: "Payment with no bill: held as customer credit (card)", badge: "LIVE", branch: "Ikeja",
  who: "You as bursar, then you as parent", money: "₦500 on a card", fees: "About 1.5%, about ₦7.50",
  note: ["Leave this ₦500 unallocated. R1 refunds all of it, and a refund can only be taken from credit."],
  steps: [
    "Payments, Money In, Collections, New checkout.",
    "Customer Emeka. Leave Invoice blank (the form says customer credit). Amount ₦500. Provider Paystack. Add Emeka's billing email and the narration \"Test advance\".",
    "Select Create checkout link. The link is copied for you.",
    "Paste it into a private window and pay by card.",
  ],
  app: [
    "The form's posting preview reads Cr 2140 Customer credit before you save.",
    "Collections: the row reads Paid.",
    "Emeka's account shows ₦500 credit. Receipts & Allocation lists an Unallocated receipt of ₦500.",
  ],
  paystack: ["Transactions: a successful ₦500 payment."],
  plain: ["The school owes Emeka's family ₦500 of credit until it is applied to a bill or refunded."],
  journal: ["Ikeja: Dr 1125 Gateway clearing ₦500 / Cr 2140 Customer Credit ₦500.", "Codex's books: held for Ikeja up by ₦500 less the fee."],
}));

add(scenario({
  id: "M7", title: "Terminal payment that matches by email (Ikeja)", badge: "LIVE", branch: "Ikeja",
  who: "You as parent; then you as Ikeja bursar to check",
  money: "₦350 on a card", fees: "Check your Paystack pricing page for terminals",
  note: [
    "Auto-matching needs exactly one active customer of Ikeja (or of every branch) with that exact email, ignoring capitals. Two customers with the same email, or none, means a person matches it (M8).",
    "The WhatsApp alert to the branch phone comes from Paystack. The app sends no notification for a terminal payment.",
  ],
  steps: [
    "Raise a ₦350 bill for Gbenga and issue it.",
    "Payments, Money In, Branch Terminals. Ikeja's card reads Open. Under How to pay copy the payment page link (or scan its code).",
    "Open the page on your phone. Enter ₦350 and Gbenga's billing email exactly as saved. Pay by card.",
    "Payments, Money In, Terminal Payments, and select the Matched filter.",
  ],
  app: [
    "The row reads \"Matched to Gbenga automatically, by the payer's email\". The Unmatched list is empty.",
    "Gbenga's invoice reads Paid.",
    "If the card says the provider has not returned a payment page yet, select Refresh details; if it is still empty, find the terminal's link in the Paystack dashboard and note it (Part 9 lists this as unconfirmed).",
  ],
  paystack: [
    "Terminals: a terminal named \"Codex Test School - Ikeja\" with a ₦350 payment. Transactions shows the same payment.",
    "Ikeja's branch phone received a WhatsApp alert.",
  ],
  plain: [
    "The money was booked to Ikeja the moment it arrived, as \"unidentified\", then moved onto Gbenga's bill when the email matched.",
  ],
  journal: [
    "On arrival, Ikeja: Dr 1125 Gateway clearing ₦350 / Cr 2195 Unidentified Receipts ₦350.",
    "On matching: Dr 2195 Unidentified Receipts ₦350 / Cr 1200 Accounts receivable ₦350.",
    "Codex's books: held for Ikeja up by ₦350 less the fee.",
  ],
}));

add(scenario({
  id: "M8", title: "Terminal payment, unknown email: match, unmatch, match again", badge: "LIVE", branch: "Ikeja",
  who: "You as parent, then you as Ikeja bursar", money: "₦350 on a card", fees: "Check your Paystack pricing page for terminals",
  note: ["The app sends no notification for a new unmatched payment. Look at the Terminal Payments tab yourself."],
  steps: [
    "Pay ₦350 on Ikeja's terminal (as in M7) using an email no customer has, for example your own second address.",
    "Payments, Money In, Terminal Payments, Unmatched. Open the new row.",
    "Under Likely customers, Bola is suggested with the reason \"Owes exactly this amount\". Select Match.",
    "Open the row under Matched. Select Unmatch and confirm.",
    "The payment is back under Unmatched. Match it to Bola again.",
  ],
  app: [
    "Before matching: the note reads \"No customer of this branch has the payer's email\", and up to five likely customers are listed with their reasons.",
    "After Match: Bola's ₦350 bill reads Paid and the row reads Matched to Bola by your name.",
    "After Unmatch: the receipt is cancelled, Bola's bill is open again at ₦350, and the payment is back under Unmatched.",
    "After the second Match the end state is the same as the first.",
  ],
  paystack: ["Terminals and Transactions: one ₦350 payment. Matching never touches Paystack."],
  plain: ["While unmatched, the money is real but nobody owns it yet: Unidentified Receipts shows ₦350 for Ikeja. It must end at zero."],
  journal: [
    "On arrival: Dr 1125 Gateway clearing ₦350 / Cr 2195 Unidentified Receipts ₦350.",
    "Each match: Dr 2195 / Cr 1200, ₦350. Unmatch reverses that receipt.",
  ],
}));

add(scenario({
  id: "M9", title: "Payment at Lekki (checkout, no bill)", badge: "LIVE", branch: "Lekki",
  who: "You as Lekki bursar, then you as parent", money: "₦1,000 on a card", fees: "About 1.5%, about ₦15",
  note: ["Do this one at the point O8 tells you, after O7. It gives Lekki enough held money for the waiting payroll line, and leaves credit for R2."],
  steps: [
    "Choose Lekki as the branch. Payments, Money In, Collections, New checkout.",
    "Customer Chidi, no invoice, Amount ₦1,000, Provider Paystack, Chidi's billing email.",
    "Create checkout link, paste it into a private window, pay by card.",
  ],
  app: [
    "Collections: the row reads Paid and shows Lekki as its branch.",
    "Chidi's account shows ₦1,000 credit.",
    "Within about ten minutes the waiting payroll line (O8) is sent.",
  ],
  paystack: ["Transactions: a successful ₦1,000 payment."],
  plain: ["Lekki now holds about ₦985 more. Ikeja's figures are untouched."],
  journal: ["Lekki: Dr 1125 Gateway clearing ₦1,000 / Cr 2140 Customer Credit ₦1,000.", "Codex's books: held for Lekki up by ₦1,000 less the fee."],
}));

add(scenario({
  id: "M10", title: "Terminal payment at Lekki (branch separation)", badge: "LIVE", branch: "Lekki",
  who: "You as parent; then Lekki bursar", money: "₦300 on a card", fees: "Check your Paystack pricing page for terminals",
  steps: [
    "Raise a ₦300 bill for Funke at Lekki and issue it.",
    "Payments, Money In, Branch Terminals. Open Lekki's card and copy its payment page link.",
    "Pay ₦300 on Lekki's terminal with Funke's billing email.",
    "Terminal Payments, Matched.",
  ],
  app: [
    "The row is matched to Funke automatically and shows Lekki as the branch. Funke's bill reads Paid.",
    "Lekki's branch phone gets the WhatsApp alert, not Ikeja's.",
    "Ikeja's own figures do not change.",
  ],
  paystack: ["Terminals: the terminal \"Codex Test School - Lekki\" has the ₦300 payment."],
  plain: ["Lekki's money is Lekki's. Lekki now holds about ₦295, which is why its payroll line waits in O7."],
  journal: [
    "On arrival, Lekki: Dr 1125 Gateway clearing ₦300 / Cr 2195 Unidentified Receipts ₦300.",
    "On matching: Dr 2195 / Cr 1200, ₦300. Codex's books: held for Lekki up by ₦300 less the fee.",
  ],
}));

add(scenario({
  id: "M11", title: "A payment that is not completed (abandoned or declined)", badge: "LIVE", branch: "Ikeja",
  who: "You as bursar, then you as parent", money: "Nothing should be charged (a bill of ₦100)", fees: "None if nothing is charged",
  note: ["Paystack reports an unpaid checkout as abandoned, and the app can overturn that later: if the payer finishes on the same link, the payment is booked after all (step 5 shows this)."],
  steps: [
    "Raise a ₦100 bill for Gbenga and issue it.",
    "Request payment, Generate payment link, Open. On Paystack's page close the tab without paying.",
    "In Collections open the row and select Re-verify.",
    "Optional, for Failed: pay with a card that has no money on it, so the bank declines it.",
    "Optional: open the same link again and pay ₦100. Leave the bill unpaid otherwise.",
  ],
  app: [
    "The row sits under Pending, or Failed (the Failed filter also covers Abandoned; the drawer says which). No receipt is booked and the bill stays open.",
    "Step 5: the row turns Paid and the audit line says it was booked after the collection was marked Abandoned.",
  ],
  paystack: ["Transactions: the ₦100 attempt reads Abandoned or Failed, or Success after step 5."],
  plain: ["Nothing is booked for a payment that did not happen. The books are unchanged until money actually arrives."],
  journal: ["None for steps 1 to 4. Step 5 books the same two lines as M1."],
}));

add(scenario({
  id: "M12", title: "The return page shows the live status", badge: "LIVE", branch: "Ikeja",
  who: "You", money: "None", fees: "None",
  note: ["This needs a school app build that carries FinPro v0.7.68 or later. An older school app build only says \"Thank you\" and promises a receipt. If you see only that for M1 or M11, note it."],
  steps: [
    "In Collections open M1's row and copy its reference.",
    "In a private window open https://<school>.xvs.codexng.com/payments/return?reference=<that reference>.",
    "Repeat with M11's reference.",
    "Repeat with a made-up reference, for example CXP-0000.",
  ],
  app: [
    "M1's reference: **Payment confirmed**, ₦500.00, \"paid to Codex Test School\", the time paid.",
    "M11's reference: **Payment not completed**, with \"This payment did not go through. You have not been charged\" for a failure, or \"This payment was not completed\" for an abandoned one.",
    "The made-up reference: the neutral \"Thank you\" with the reference quoted. It must not claim success, and must not reveal who or what it is for.",
    "The page shows nothing about the customer, invoice, email or branch.",
  ],
  paystack: ["Nothing to check."],
  plain: ["The page only repeats what the server says about the payment. It never decides from the redirect alone."],
  journal: ["None."],
}));

/* =====================================================================
 * PART 4. MONEY BACK
 * ===================================================================== */
add(h1("Part 4. Money back"));
add(body("A refund returns customer credit to the payer. It can only be taken from credit: a payment that was already applied to a bill leaves no credit to refund. That is why M5, M6 and M9 left credit behind. Every refund needs a second person to approve it, whatever the amount."));
add(box("How a refund through Paystack runs", [
  "1. A bursar raises the refund with \"Back through Paystack\" and picks the original payment. 2. A second person approves it. 3. On approval the app takes the amount out of the branch's held money and asks Paystack. 4. When Paystack confirms, the books move the credit out. If Paystack fails it, the held money is given back and the refund is a draft again.",
  "A refund can be taken only from the branch's own held money, never more than the payment has left, and only for a customer and branch that match the payment. Direct-custody schools never see this option (Part 8).",
]));

add(scenario({
  id: "R1", title: "Refund through Paystack, the whole payment", badge: "LIVE", branch: "Ikeja",
  who: "You as Ikeja bursar; your second person approves", money: "None (₦500 comes back to your card)", fees: "None. Paystack keeps the fee it took on M6.",
  note: ["Card refunds can take from minutes to several days to reach the payer (confirm with Paystack). The app re-checks by itself every 15 minutes at most."],
  steps: [
    "Receivables, Adjustments, Refunds & Write-offs, New action.",
    "Action: Refund to bank. Under Pay the refund choose **Back through Paystack**. Customer Emeka (only customers with refundable credit are listed).",
    "Original payment: choose M6's ₦500 payment. Amount ₦500. Reason \"Test: full refund\".",
    "Next action: Submit for approval, then select Submit for approval.",
    "Your second person opens Workflow, Approvals, selects Review on the refund and approves.",
    "Open the refund again and watch the panel headed Returned through Paystack.",
  ],
  app: [
    "After approval the panel reads **Processing**, then **Returned** when Paystack confirms. Select Check with Paystack to ask sooner.",
    "Emeka's credit falls from ₦500 to ₦0.",
    "Online payments panel: Ikeja's held amount fell by ₦500 as soon as the refund was approved.",
  ],
  paystack: ["Refunds (under Transactions): a ₦500 refund against M6's payment, then processed. Your card or account receives ₦500."],
  plain: [
    "Emeka's family is owed nothing. The money left the held online payments, not a bank account.",
    "The school pays no extra fee for the refund. Paystack keeps the fee from the original payment.",
  ],
  journal: [
    "Ikeja, when Paystack confirms: Dr 2140 Customer Credit ₦500 / Cr 1125 Gateway clearing ₦500.",
    "Codex's books: held for Ikeja down ₦500 when the refund was approved.",
  ],
}));

add(scenario({
  id: "R2", title: "Partial refund through Paystack (and the over-limit refusal)", badge: "LIVE", branch: "Lekki",
  who: "You as Lekki bursar; your second person approves", money: "None (₦200 comes back)", fees: "None",
  steps: [
    "After O8, raise a refund as in R1 for customer Chidi, original payment M9's ₦1,000, Amount ₦200. Submit, approve.",
    "Watch it reach Returned.",
    "Raise a second refund for ₦900 against the same payment and try to save it.",
  ],
  app: [
    "The ₦200 refund follows R1. Chidi's credit falls from ₦1,000 to ₦800.",
    "The ₦900 refund is refused: it is more than the ₦800 the payment has left. Cancel it.",
    "The payment itself still reads Paid. The app has no Refunded state for a part refund.",
  ],
  paystack: ["Refunds: one ₦200 partial refund against the ₦1,000 transaction."],
  plain: ["Only ₦200 has left. The rest is still Chidi's credit."],
  journal: ["Lekki: Dr 2140 Customer Credit ₦200 / Cr 1125 Gateway clearing ₦200.", "Codex's books: held for Lekki down ₦200."],
}));

add(scenario({
  id: "R3", title: "Refund paid from the bank instead", badge: "RECORD", branch: "Ikeja",
  who: "You as Ikeja bursar; your second person approves", money: "None to Paystack. Pay the family from your own bank if you wish", fees: "None",
  steps: [
    "Receivables, Adjustments, Refunds & Write-offs, New action. Action: Refund to bank. Under Pay the refund choose **From the bank**.",
    "Customer Dayo (credit of ₦250 left from M5). Refund to bank account: Ikeja's account. Amount ₦100. Reason \"Test: refund from bank\".",
    "Submit for approval. Your second person approves. The refund posts.",
  ],
  app: [
    "The refund reads Posted. Dayo's credit falls to ₦150.",
    "Nothing under Payments or Refunds changes. No Paystack panel appears on this refund.",
  ],
  paystack: ["Nothing. If you see a Paystack entry, stop and note it."],
  plain: ["The school records that it paid ₦100 back to the family from Ikeja's bank. Match the bank line later in bank reconciliation."],
  journal: ["Ikeja: Dr 2140 Customer Credit ₦100 / Cr Cash & Bank (Ikeja's bank ledger account) ₦100."],
}));

add(scenario({
  id: "R4", title: "A refund Paystack may refuse (a payment made by transfer)", badge: "LIVE", branch: "Ikeja",
  who: "You as Ikeja bursar; your second person approves", money: "None (₦100 may come back)", fees: "None",
  note: [
    "Paystack has not confirmed whether a payment made by bank transfer into a dedicated account can be refunded. Both outcomes are fine here. Record which one you got.",
    "A refund waiting for the payer's bank details shows as Processing in the app, and the app has no screen to supply them. If it stays Processing for days, look at the refund in the Paystack dashboard.",
  ],
  steps: [
    "Raise a refund as in R1 for customer Dayo, original payment M5's ₦700 transfer, Amount ₦100. Submit, approve.",
    "Open the refund and read the panel headed Returned through Paystack until it settles.",
    "If it shows Failed: read \"Why it failed\", then try Try again once, and then Pay from bank instead (choose Ikeja's account, confirm Pay from bank).",
  ],
  app: [
    "Outcome A: Processing, then Returned. Dayo's credit falls by ₦100.",
    "Outcome B: **Failed** with a reason in red. The refund is a draft again, Ikeja's held amount has been given back, and the buttons Try again and Pay from bank instead appear. Pay from bank instead turns it into a bank refund, which then needs approval again.",
  ],
  paystack: ["Refunds: the ₦100 refund against the transfer, with Paystack's own status (pending, processed, failed or needs attention)."],
  plain: ["Outcome A: as R1 with ₦100. Outcome B: nothing is booked, and the held money is back where it was."],
  journal: [
    "Outcome A: Ikeja: Dr 2140 Customer Credit ₦100 / Cr 1125 Gateway clearing ₦100.",
    "Outcome B: none until you choose Pay from bank instead.",
  ],
}));

add(scenario({
  id: "R5", title: "A dispute or chargeback", badge: "WATCH", branch: "Whichever branch took the payment",
  who: "Finance staff and Codex operators", money: "A chargeback takes the payment back from your balance", fees: "Check your Paystack pricing page for dispute fees",
  note: ["You cannot force a dispute. Do not ask your bank to charge back a test payment. Leave the box at Not run unless one arrives."],
  steps: [
    "If Paystack emails you about a dispute, open Payments, Monitoring, Needs Attention and Payments, Monitoring, Transactions Log and find the payment by its reference.",
    "The people with the right to see it are told: the school's finance staff who can see payment events, and Codex's operators.",
    "Answer the dispute in the Paystack dashboard before its deadline. Do not refund the same payment as well.",
    "When Paystack decides, check the app again.",
  ],
  app: [
    "On a payment Codex held, the branch's held amount falls at once by what the payer's bank took, once only. If the branch held less, the shortfall is what the branch owes Codex and is taken from its next payments before any settlement.",
    "The receipt and bill are left as they are. Finance staff chase the payer.",
    "If Paystack decides for you (declined), the held amount is given back once. If the decision is against you, it stays as booked.",
    "A school paid direct only gets the dispute recorded and raised to people. Nothing is booked.",
  ],
  paystack: ["Disputes: the case, its deadline and its decision."],
  plain: ["The school has lost the money and the books say so. Winning the dispute puts it back."],
  journal: [
    "School, when the chargeback arrives: Dr 5520 Payment Chargebacks / Cr 1125 Gateway clearing, in the branch.",
    "Codex's books: Dr 2180 Client Funds Held (and Dr 1128 Owed by Clients for any shortfall) / Cr 1127 Payment Provider Balance.",
    "Won: both are reversed once.",
  ],
}));

/* =====================================================================
 * PART 5. MONEY OUT
 * ===================================================================== */
add(h1("Part 5. Money out"));
add(body("Money goes out of Paystack only after a second person approves it. A branch can send only what Codex holds for it, plus Paystack's transfer fee (about ₦10 up to ₦5,000), which the branch bears. The fee is reserved with the amount and booked once the transfer is paid."));
add(box("How a payout reaches the books", [
  "A payout is booked **Dr the supplier's account or net wages payable, Cr 1125 Gateway clearing**, not the bank: the money left the Paystack balance, which the branch carries as gateway clearing. The fee is booked as **Dr 5500 Bank Charges, Cr 1125 Gateway clearing**. Codex's books lower the branch's held amount by the amount plus the fee when the transfer is sent.",
  "A payout that is more than the branch holds is refused and marked Failed, with nothing sent. Only payroll lines wait for funds instead (O7).",
]));

add(scenario({
  id: "O1", title: "Vendor payout, no tax withheld", badge: "LIVE", branch: "Ikeja",
  who: "You as bursar (prepare); your second person approves", money: "None (₦500 goes to your own account)", fees: "About ₦10 Paystack transfer fee, borne by Ikeja",
  note: [
    "At a school with two branches, the branch is taken from the From bank account you pick. Pick Ikeja's account. Leaving it empty can leave the payout with no branch, and it is refused.",
    "When you save, the app says \"Payout saved. It is sent once it is approved.\" Nothing is sent until the second person approves.",
    "The vendor must be Active, not on hold, KYC status Verified, and have bank name, account number, account name and a bank chosen from the list.",
  ],
  steps: [
    "Procurement, Vendors, Add Vendor. Company name \"Test Vendor Ltd\". Under Bank details enter your account X (choose the bank from the list), then Create Vendor. Edit Vendor, Status & Governance: KYC status Verified.",
    "Recommended: record and post a ₦500 bill for this vendor first, at Ikeja (see B11). A payout settles only the open bills of its own branch. With no open bill it is booked as a payment in advance instead.",
    "Payments, Money Out, Payouts, New payout. Choose the vendor (its bank details fill in), Amount ₦500, From bank account: Ikeja's account. Select Send payout.",
    "Your second person opens Workflow, Approvals, Review, Approve.",
    "Open the payout and watch its steps until it reads Settled. Check your bank app.",
  ],
  app: [
    "The payout row reads Pending, then **Settled**. Its steps read Payout created, Sent to provider, Settled.",
    "Online payments panel: Ikeja's held amount fell by ₦510.",
    "The vendor's bill (if you recorded one) reads Paid.",
  ],
  paystack: ["Transfers: a successful ₦500 transfer to your account X, with a CXP- reference. The transfer fee is shown on it."],
  plain: [
    "The school paid the vendor ₦500 from the money Codex holds for Ikeja.",
    "Ikeja also paid a ₦10 bank charge. With no bill, the books show a payment in advance (Vendor Advances, 1240) rather than a reduced supplier balance.",
  ],
  journal: [
    "Ikeja: Dr 2100 Accounts payable ₦500 / Cr 1125 Gateway clearing ₦500.",
    "Fee: Dr 5500 Bank Charges ₦10 / Cr 1125 Gateway clearing ₦10.",
    "Codex's books: held for Ikeja down ₦510.",
  ],
}));

add(scenario({
  id: "O2", title: "Vendor payout with withholding tax (sent amount is less)", badge: "LIVE", branch: "Ikeja",
  who: "You as bursar; your second person approves", money: "None (₦950 goes to your own account)", fees: "About ₦10 transfer fee on the ₦950 sent",
  note: [
    "The New payout form has no tax box. The tax comes from the vendor's Default withholding tax code. Without that code no tax is taken. The app does not read the tax off a bill.",
    "Sent amount = payout amount minus tax. The supplier receives ₦950, not ₦1,000, and the books keep the ₦50 as tax owed to the tax office.",
  ],
  steps: [
    "Edit the vendor: Accounting defaults, Default withholding tax code: WHT-5.",
    "Record and post a ₦1,000 bill for the vendor (B11), or accept a payment in advance.",
    "Payments, Money Out, Payouts, New payout. Vendor, Amount ₦1,000, From bank account: Ikeja's account. Send payout.",
    "Your second person approves. Wait until Settled.",
  ],
  app: [
    "The payout row shows ₦1,000. The transfer that left Paystack is ₦950.",
    "Ikeja's held amount fell by ₦960 (₦950 sent plus the ₦10 fee).",
  ],
  paystack: ["Transfers: a ₦950 transfer to your account."],
  plain: [
    "The vendor was owed ₦1,000. The school paid ₦950 and keeps ₦50 owed to the tax office (WHT payable).",
    "Payments, Settlement may later show this payout as costing a fee equal to the tax; that is a known display quirk (Part 9).",
  ],
  journal: [
    "Ikeja: Dr 2100 Accounts payable ₦1,000 / Cr 1125 Gateway clearing ₦950 / Cr 2300 WHT payable ₦50.",
    "Fee: Dr 5500 Bank Charges ₦10 / Cr 1125 Gateway clearing ₦10.",
  ],
}));

add(scenario({
  id: "O3", title: "Payroll set-up: choose Through Paystack", badge: "RECORD", branch: "Whole school",
  who: "School administrator", money: "None", fees: "None",
  note: [
    "Through Paystack can be chosen only while Codex holds the school's online money. At a school paid direct, the choice is closed and the screen says why.",
    "Once chosen, a switch of the school to direct custody is refused until you set the route back (Part 8).",
  ],
  steps: [
    "Finance Settings, Payroll, \"How staff are paid\". Choose **Through Paystack**.",
    "Select Save how staff are paid.",
    "Check the payroll approver group has your second person (2.5).",
  ],
  app: [
    "The choice saves. The page reads \"How staff are paid\" with Through Paystack selected.",
    "Reload the page: the choice is still selected.",
  ],
  paystack: ["Nothing."],
  plain: ["From now on, a posted payroll run is paid by Paystack transfer to each person's saved bank account."],
  journal: ["None."],
}));

add(scenario({
  id: "O4", title: "Staff bank account whose name matches", badge: "LIVE", branch: "Ikeja",
  who: "Ikeja bursar (or administrator)", money: "None", fees: "None known (Paystack's account-name lookup)",
  note: ["The rule is on words, not spelling: the holder's name must contain the staff member's surname and at least one other of their names, in any order. \"ADEYEMI TUNDE OLAWALE\" matches Tunde Adeyemi."],
  steps: [
    "Payroll, Bank accounts tab, Add a staff account.",
    "Choose Staff A (the staff list shows only people on the payroll with no account yet). Continue.",
    "Choose the bank and type account X's number. Select Save and check.",
  ],
  app: [
    "Paystack is asked who holds the account. The row shows the name Paystack returned and the pill **Checked**.",
    "The detail reads \"Paystack named the account's holder and the name matches. Payroll can pay it.\"",
  ],
  paystack: ["Nothing shows on the dashboard for a name lookup."],
  plain: ["Staff A can now be paid by Paystack. No money moved."],
  journal: ["None."],
}));

add(scenario({
  id: "O5", title: "Staff bank account whose name does not match: a second person confirms", badge: "LIVE", branch: "Lekki",
  who: "Lekki bursar saves it; your second person confirms", money: "None", fees: "None known",
  note: [
    "The person who saved the account cannot confirm it, and neither can the staff member. Confirm only after asking the staff member that the account is theirs.",
    "Any later change to the bank or number starts the check again, so a checked account cannot be quietly pointed elsewhere.",
  ],
  steps: [
    "Payroll, Bank accounts tab, Add a staff account. Choose Staff B. Enter your account Y (its holder name is yours, not Staff B's). Save and check.",
    "Read the pill. It must say **Needs a second person**. Try to confirm it yourself: there is no Confirm button for you (if you hold the confirm key, a note says a second person must confirm).",
    "Your second person opens the account and selects Confirm, then Confirm account.",
  ],
  app: [
    "Before: \"Needs a second person\". The row shows Paystack's holder name. Nothing is paid to it.",
    "After: the pill reads Checked (\"Confirmed by a second person\" in the filter), and the drawer shows who confirmed and when.",
  ],
  paystack: ["Nothing."],
  plain: ["Staff B can now be paid. No money moved."],
  journal: ["None."],
}));

add(scenario({
  id: "O6", title: "Prepare, send for approval, approve and post a payroll run", badge: "RECORD", branch: "Whole school (Ikeja and Lekki staff)",
  who: "You as a whole-school finance user (prepare, send, post); your second person approves", money: "None", fees: "None",
  note: ["Posting and paying a whole-school run are for someone who covers the whole school; a branch bursar sees only their branch's part. The preparer and the person who sends a run for approval can never approve it. A route only they could approve is refused when you send it."],
  steps: [
    "Payroll, Payroll runs, New payroll run. Keep \"From roster\". Period \"October 2026\", Payment date today. Choose the whole school. Generate run.",
    "Open the run. Check each person's net pay. Staff A, Staff B and Staff C are on it. Write the net figures here: A ₦_______ B ₦_______ C ₦_______.",
    "Select Submit for approval, then Send for approval.",
    "Your second person opens Workflow, Approvals, Review, Approve.",
    "Back in the run, select Calculate & post. The run reads Calculated.",
  ],
  app: [
    "The run reads Draft, then Awaiting approval, then Approved, then **Calculated**.",
    "While it waits you cannot change it. If the approver sends it back, only the person who sent it can correct it.",
    "The run shows, per branch, what each person is owed.",
  ],
  paystack: ["Nothing."],
  plain: [
    "The school now owes each person their net pay, and owes the tax office and pension administrators their shares. No money has moved.",
  ],
  journal: [
    "Branch by branch: Dr 5200 Salaries & Wages (gross, plus employer pension, NSITF and ITF costs) / Cr 2330 Net Wages Payable (net pay) / Cr 2310 PAYE Payable / Cr 2320 Pension Payable (and the other payables).",
  ],
}));

add(scenario({
  id: "O7", title: "Pay through Paystack: watch each line, and a branch that is short waits", badge: "LIVE", branch: "Ikeja and Lekki",
  who: "You as a whole-school finance user", money: "None (net pay comes to your accounts)", fees: "About ₦10 per transfer, borne by each staff member's branch",
  note: [
    "A payroll batch is sent one branch at a time, from that branch's held money. If a branch holds less than its whole batch needs (fees included), nothing of that batch is sent and its lines wait. The other branch is paid at once.",
    "Lekki holds about ₦295 now (M10), so Staff B's line must wait. If Lekki holds more, you are in the wrong order: see 2.1.",
  ],
  steps: [
    "Open the run (it reads Calculated). Select **Pay through Paystack**, then Pay staff.",
    "Watch the panel \"Paying staff through Paystack\" and its sentence, for example \"1 of 3 paid. 1 waiting for funds, 1 need bank details.\" Select Refresh every minute or two.",
    "Check the counts: Paid, Sent, Failed, Waiting for funds, Needs bank details, Not sent yet.",
  ],
  app: [
    "Staff A (Ikeja): Sent, then **Paid**. A payslip is issued for a paid line.",
    "Staff B (Lekki): **Waiting for funds**, with a note why.",
    "Staff C (Ikeja): **Needs bank details**.",
    "The branch table lists each branch's staff, net pay, paid so far and batch status.",
  ],
  paystack: ["Transfers: one transfer, to Staff A's account X, for A's net pay. None yet for B or C."],
  plain: [
    "Ikeja's staff were paid from Ikeja's held money. Lekki's money was too little, so nobody at Lekki was half-paid.",
    "No notification is sent when a payroll payout fails or waits. Watch this panel.",
  ],
  journal: [
    "For each paid line, in the line's branch: Dr 2330 Net Wages Payable (net pay) / Cr 1125 Gateway clearing (net pay).",
    "Fee for each: Dr 5500 Bank Charges ₦10 / Cr 1125 Gateway clearing ₦10.",
    "Codex's books: held for Ikeja down by A's net plus ₦10.",
  ],
}));

add(scenario({
  id: "O8", title: "The waiting payroll line is released by a Lekki payment", badge: "LIVE", branch: "Lekki",
  who: "You", money: "M9's ₦1,000", fees: "About ₦10 transfer fee",
  steps: [
    "Now do M9 (the ₦1,000 Lekki payment).",
    "Wait up to ten minutes. The app tries waiting payroll lines again every ten minutes.",
    "Open the run and refresh the panel.",
    "Then do R2 (partial refund at Lekki).",
  ],
  app: [
    "Staff B moves from Waiting for funds to Sent, then **Paid**. A payslip is issued.",
    "Staff C is still Needs bank details, so the run does not read Paid yet.",
  ],
  paystack: ["Transfers: a transfer to your account Y for Staff B's net pay."],
  plain: ["Lekki's staff member was paid as soon as Lekki had enough money. Nothing was sent twice."],
  journal: [
    "Lekki: Dr 2330 Net Wages Payable (B's net) / Cr 1125 Gateway clearing.",
    "Fee: Dr 5500 Bank Charges ₦10 / Cr 1125 Gateway clearing ₦10.",
  ],
}));

add(scenario({
  id: "O9", title: "Record a line as paid from the bank", badge: "RECORD", branch: "Ikeja",
  who: "You as bursar (needs the pay-payroll key and cover of the whole run)", money: "None to Paystack", fees: "None",
  note: ["A line that is Waiting for funds cannot be recorded this way. Only lines that are Not sent yet, Need bank details, or Failed can."],
  steps: [
    "In the payroll run's payment panel, tick Staff C's line (Needs bank details).",
    "Select Record as paid from the bank.",
    "Choose Ikeja's bank account and the payment date. Select Record as paid.",
    "Optional variation, if you have a spare account: instead add a bank account for Staff C (O4) and select \"Send the rest through Paystack\". Only the held-back line is sent.",
  ],
  app: [
    "Staff C's line reads Paid. The run now reads **Paid**.",
    "One branch's lines must be recorded together, from that branch's own bank.",
  ],
  paystack: ["Nothing. If you used the variation, one more transfer."],
  plain: ["The school says it paid Staff C itself from Ikeja's bank. Match the bank line later in bank reconciliation."],
  journal: ["Ikeja: Dr 2330 Net Wages Payable / Cr Cash & Bank (Ikeja's bank ledger account), for C's net pay."],
}));

add(scenario({
  id: "O10", title: "Retry a failed payroll line", badge: "WATCH", branch: "The failed line's branch",
  who: "You as bursar", money: "A retry sends the net pay again", fees: "About ₦10 for the new attempt",
  note: ["You cannot force a failure. If every line in O7 and O8 paid, leave Not run."],
  steps: [
    "If a line reads Failed, read the reason in the Why column. A common cause is a wrong account or bank.",
    "Fix the cause (for example correct the staff bank account and let Paystack check it again).",
    "Select Retry failed (the number shows how many).",
  ],
  app: [
    "The line returns to Sent, then Paid. A failed transfer gave its held money back, so the retry does not double-spend it.",
    "The staff member's bank details cannot have changed since the payment was prepared; if they did, the line asks you to pay it again.",
  ],
  paystack: ["Transfers: the failed attempt, then the new one."],
  plain: ["A failed line costs nothing and moves no money until a retry succeeds."],
  journal: ["Only the successful attempt posts, as in O8."],
}));

add(scenario({
  id: "O11", title: "Payroll paid from the school's own bank", badge: "RECORD", branch: "Whole school",
  who: "School administrator; the bursar; your second person", money: "None to Paystack", fees: "None",
  note: [
    "The same staff cannot be paid twice for one month, so use a pay date in a month not yet paid (the next month), or a school that already pays from its bank.",
    "Do this before D1: a school cannot move to direct custody while the route is Through Paystack.",
  ],
  steps: [
    "Finance Settings, Payroll, How staff are paid: choose **From the school's bank**. Save.",
    "Payroll, Payroll runs, New payroll run: Period for next month, Payment date in that month. Generate, Submit for approval, approve, Calculate & post (as O6).",
    "Open the run and select **Pay net**. Choose the payment date, and for each branch the bank account it is paid from. Confirm.",
  ],
  app: [
    "Pay net appears (there is no Pay through Paystack button). Nothing is sent to Paystack.",
    "The run reads Paid once every branch is paid. Payslips are issued.",
  ],
  paystack: ["Nothing. If a transfer appears, stop and note it."],
  plain: ["The school records that it paid its staff from each branch's own bank. Someone must make the bank transfers once and match them in bank reconciliation."],
  journal: ["Per branch: Dr 2330 Net Wages Payable / Cr Cash & Bank (that branch's bank ledger account)."],
}));

add(scenario({
  id: "O12", title: "Daily settlement of held money to each branch's bank", badge: "LIVE", branch: "Ikeja and Lekki",
  who: "Codex finance operator submits; two Codex approvers approve", money: "None (the held money is paid into your accounts X and Y)", fees: "About ₦10 per settlement transfer, borne by the branch",
  note: [
    "A settlement claims every payment confirmed before today, so do this the morning after Day 1. It sends the money Codex holds for the branch, capped at the branch's held balance. Refunds and payouts already used part of it.",
    "A branch whose held money would not cover the transfer fee is left for a later run.",
    "The school's own people do nothing: they only watch. Neither they nor the operator can skip the two Codex approvers.",
  ],
  steps: [
    "Codex finance operator: Payments, Platform Controls, Held Settlements. A row per branch reads **Ready to submit**. Open the Ikeja row and check Payments covered, Provider fees, Transfer fee and To send.",
    "Select Submit for approval, then confirm. The row reads Waiting for approval.",
    "Two different Codex approvers open Workflow, Approvals and approve. The row reads Approved, sending, then **Paid**.",
    "Repeat for Lekki.",
    "School side: Payments, Platform Controls, Held Settlements shows the school's own settlements read-only. Check your bank apps for X and Y.",
  ],
  app: [
    "Settled amount = payments covered, less provider fees, capped at what the branch still holds, less the ₦10 transfer fee.",
    "With fewer than two distinct real approvers the settlement will not be sent.",
    "At the school nobody submitted or approved anything. Its movements feed shows each settlement as a transfer into its bank.",
    "After both are paid, Gateway clearing for each branch should be close to zero (only newer payments remain).",
  ],
  paystack: ["Transfers: one transfer to account X (Ikeja) and one to account Y (Lekki), each with a CXP- reference."],
  plain: [
    "Codex paid the school the money it was holding. The branch bore the transfer fee.",
    "In Codex's books the held amount for each branch is now zero.",
  ],
  journal: [
    "Each school branch, when Paystack confirms: Dr bank (the amount that arrived) / Dr 5500 Bank Charges (payment fees plus the transfer fee) / Cr 1125 Gateway clearing (the total).",
    "Codex's books, when the transfer is sent: Dr 2180 Client Funds Held / Cr 1127 Payment Provider Balance.",
  ],
}));

add(scenario({
  id: "O13", title: "Codex's own payout, within its own share", badge: "LIVE", branch: "Codex's own books (Lagos)",
  who: "Codex finance operator; Codex payout approver", money: "None (₦200 goes to your own account)", fees: "About ₦10 transfer fee",
  note: [
    "Codex's own payouts may use only Codex's own share: the Paystack balance, less everything held for schools, less transfers in flight. Your ₦3,500 top-up is that share.",
    "Codex needs a vendor, a bank account and a payout approver of its own. If creating the payout says no approval route was found, see 2.4.",
  ],
  steps: [
    "In the Codex console choose Codex's books. Payments, Money Out, Payouts, New payout.",
    "Choose a Codex vendor (Verified, bank details saved), Amount ₦200, From bank account: Codex's account. Send payout.",
    "The Codex payout approver approves in Workflow, Approvals. Wait until Settled.",
  ],
  app: [
    "The payout reads Settled. No school's held amount changes.",
    "The daily check (C1) still agrees: it counts Codex's own payouts as well as the booked top-up.",
  ],
  paystack: ["Transfers: a successful ₦200 transfer."],
  plain: ["Codex paid a supplier out of its own money, not a school's."],
  journal: ["Codex's books: Dr Accounts payable ₦200 / Cr Gateway clearing ₦200, plus the fee lines as O1."],
}));

add(scenario({
  id: "O14", title: "Codex's own payout larger than its share", badge: "LIVE", branch: "Codex's own books (Lagos)",
  who: "Codex finance operator; Codex payout approver", money: "None", fees: "None, because nothing is sent",
  note: ["You might expect such a payout to wait until there is money. It does not: the payout is refused, marked Failed with a reason, and nothing is sent. Only payroll lines wait. Record what you see."],
  steps: [
    "As O13, raise a payout of ₦5,000 (more than the ₦3,500 share). Send payout and have it approved.",
    "Open the payout.",
  ],
  app: [
    "The payout reads **Failed** with a reason like \"The platform's own share of the provider balance is ₦X and this transfer needs ₦Y. The rest is held for schools.\"",
    "If Paystack's balance could not be read, the reason says the share could not be confirmed.",
  ],
  paystack: ["Transfers: no new transfer."],
  plain: ["Codex cannot spend schools' money on its own bills. Nothing was booked."],
  journal: ["None."],
}));

/* =====================================================================
 * PART 6. CHECKS AND CONTROLS
 * ===================================================================== */
add(h1("Part 6. Checks and controls"));
add(body("These tests prove the safety rules: the daily balance check, that nobody approves their own work, and that one branch cannot see or spend another's money."));

add(scenario({
  id: "C1", title: "The daily held-ledger check against the Paystack balance", badge: "LIVE", branch: "Codex's books",
  who: "Codex finance operator", money: "None", fees: "None",
  note: [
    "Codex's books say what the Paystack balance should be. Paystack says what it is. A booked top-up is counted, so with C7 done the check should agree. Before the top-up is booked, a difference about equal to it is expected. Money in Paystack that is in no book still shows as a difference: compare with the figure you wrote in 2.3.",
    "A difference above the tolerance (default ₦0) opens one health incident and tells Codex's health and settlement operators once. The next day that agrees resolves it.",
  ],
  steps: [
    "Payments, Platform Controls, Held Reconciliations (the check runs about 07:15 each day).",
    "Find today's row: Day, Provider, Provider says, Books say, Difference, Result. Open it for the parts.",
    "Compare Provider says with the Balance shown in your Paystack dashboard at the same moment.",
    "Confirm the switch \"Paystack balance swept automatically\" on this page reads Off.",
  ],
  app: [
    "Provider says equals the dashboard balance (allow for anything that moved since 07:15).",
    "Books say is the provider balance account (which mirrors the held money for all schools) plus Codex's own online takings still in transit. \"Held for schools' branches\" shows the total held.",
    "The Top-ups column shows the booked top-ups. Difference is nil (within the tolerance) and the Result agrees. Codex's own payouts (O13) are counted, so they do not cause a difference. A day before C7 was booked reads as disagreeing by about the top-up.",
    "A day Paystack could not be asked is recorded with its error and changes nothing.",
  ],
  paystack: ["Balance: the figure Provider says should match."],
  plain: ["This is the alarm that tells Codex its records and Paystack's disagree. An unexplained difference is the one to worry about."],
  journal: ["None. The check reads the books and writes only its own record."],
}));

add(scenario({
  id: "C2", title: "The person who prepared a payroll run cannot approve it", badge: "RECORD", branch: "Whole school",
  who: "You as preparer; then your second person", money: "None", fees: "None",
  note: ["Do this with the draft run of O11, before it is approved."],
  steps: [
    "Prepare and send the O11 run for approval (as O6). Do not approve it.",
    "Open Workflow, Approvals as yourself. The run is not in your queue.",
    "Open the run from Workflow, Manage Approvals. You are offered no Approve or Reject buttons.",
    "Variation: take your second person out of the payroll approver group, then send a run. The app says nobody can approve it yet, or refuses at sending, and names the group to fill. Put the person back.",
    "Your second person approves.",
  ],
  app: [
    "Step 3: no decision buttons for you (a direct attempt is refused as \"not an eligible approver for this stage\"). The person who sent it is barred too.",
    "Step 4: a message like \"Nobody can approve this run yet ... The person who prepared a run can never approve it.\"",
  ],
  paystack: ["Nothing."],
  plain: ["No one can pay staff on their own say-so."],
  journal: ["None."],
}));

add(scenario({
  id: "C3", title: "A branch bursar cannot see another branch's terminal payments", badge: "RECORD", branch: "Ikeja and Lekki",
  who: "Ikeja bursar (a user who works only in Ikeja)", money: "None", fees: "None",
  steps: [
    "Sign in as the Ikeja bursar. Payments, Money In, Terminal Payments. Choose Matched, then All.",
    "Look for M10's ₦300 payment (Lekki).",
    "Payments, Money In, Branch Terminals.",
    "Optional: make one more ₦100 payment at Lekki's terminal with an unknown email, then as the Lekki bursar try to match it to Bola (an Ikeja customer).",
  ],
  app: [
    "Only Ikeja's payments are listed. There is no branch chooser, and M10 is not there.",
    "Only Ikeja's terminal card is shown.",
    "The optional match: Bola is not offered in the picker for Lekki's payment, and the server refuses with \"A payment can only be matched to a customer of its own branch\".",
  ],
  paystack: ["Nothing."],
  plain: ["Each branch sees and matches only its own money. A payment cannot settle another branch's bills."],
  journal: ["None."],
}));

add(scenario({
  id: "C4", title: "A direct-custody school is offered no Paystack refund or payout", badge: "RECORD", branch: "Whole school",
  who: "Any bursar", money: "None", fees: "None",
  note: ["This needs the school on direct custody, so it is done as D5. Tick it here when D5 passes."],
  steps: ["Do D5 after D1 has completed."],
  app: ["See D5."],
  paystack: ["Nothing."],
  plain: ["At a school paid direct, the money is already in the branch's bank, so it pays and refunds from there."],
  journal: ["None."],
}));

add(scenario({
  id: "C5", title: "A payout above the branch's held money is refused", badge: "LIVE", branch: "Lekki",
  who: "Lekki bursar; your second person approves", money: "None (nothing is sent)", fees: "None",
  note: ["Do this on Day 2 after O12, when Lekki holds almost nothing."],
  steps: [
    "Payments, Money Out, Payouts, New payout. Vendor Test Vendor Ltd, Amount ₦200, From bank account: Lekki's account. Send payout.",
    "Your second person approves.",
    "Open the payout.",
  ],
  app: [
    "The payout reads **Failed** with a reason like \"Lekki has ₦0 of online payments held by the platform, and this transfer needs ₦210\" and the advice to wait for more payments or pay from the bank.",
    "Nothing leaves Paystack and Lekki's held amount does not go below zero.",
  ],
  paystack: ["Transfers: no new transfer."],
  plain: ["A branch cannot spend money it does not hold."],
  journal: ["None."],
}));

add(scenario({
  id: "C6", title: "The person who asked for a payout cannot approve it", badge: "RECORD", branch: "Ikeja",
  who: "You, then your second person", money: "None (the payout is left unapproved, then withdrawn)", fees: "None",
  steps: [
    "Raise any payout of ₦100 from Ikeja's account (as O1) and send it.",
    "Open Workflow, Approvals as yourself. Look for it. Then open it from Workflow, Manage Approvals: you are offered no Approve or Reject buttons.",
    "Open Workflow, My Submissions and withdraw it, so nothing is sent.",
  ],
  app: [
    "It is not in your Pending Approvals, and a direct attempt is refused as \"not an eligible approver\".",
    "Withdrawing it leaves the payout unsent.",
  ],
  paystack: ["Transfers: nothing new."],
  plain: ["Nothing leaves without two people. The app also checks again at the moment of sending, so a weakened approval route cannot send money alone."],
  journal: ["None."],
}));


add(scenario({
  id: "C7", title: "Record a Codex top-up and see the daily check agree", badge: "LIVE", branch: "Codex's books (the branch of the bank account used)",
  who: "Codex finance operator records it; two other Codex approvers approve", money: "₦3,500 from a Codex bank account into Paystack", fees: "Check Paystack for any charge on adding money. The app books none",
  note: [
    "The recorder cannot approve. Two other real Codex people from the held settlement approver group must, one vote each. The same person approving twice, or the recorder approving, books nothing.",
    "The top-up is dated the day the money left the bank (not in the future) and books into that bank account's branch, in a month that branch has open. A reference already used on another top-up is refused.",
    "Codex needs a bank account entered in its own books, and the operator needs the top-up keys (payments.platform_topup view, create and submit). A school user never sees this screen.",
  ],
  steps: [
    "Add ₦3,500 to your Paystack balance from a Codex bank account, using the Paystack dashboard's way of adding money. Note the bank reference.",
    "In the Codex console open Payments, Platform Controls, Paystack Top-ups, then New top-up.",
    "Fill in Amount ₦3,500, Date the money left the bank, Paid from (Codex's own bank account), Provider Paystack, the Reference and a Note. Select Save top-up.",
    "Open the top-up, select Submit for approval, then Submit. If it warns that nobody can approve it yet, put two people in the approver group first.",
    "Two other Codex approvers open Workflow, Approvals, Review and approve.",
    "Open the top-up again and read its status and journal.",
    "Next morning open Payments, Platform Controls, Held Reconciliations and read the new row.",
  ],
  app: [
    "The top-up reads Draft, then Waiting for approval, then **Booked**, with its journal and booking time. Before the second approval nothing is booked.",
    "Held Reconciliations: the row's Top-ups column shows ₦3,500, Difference is nil, and Result agrees. A disagreement the day before booking has resolved itself.",
    "Held for schools' branches does not change: a top-up is owed to no school.",
  ],
  paystack: ["Balance: it includes the ₦3,500 (the dashboard may call the page Balance or Balance history)."],
  plain: [
    "Codex put its own money into Paystack. It is owed to no school, so no branch's held amount rises. Codex's own share, which O13 and O14 spend from, rises by ₦3,500.",
    "Booking it lets the daily check expect that money, so the check agrees with Paystack again.",
  ],
  journal: [
    "Codex's books, in the bank account's branch: Dr 1127 Payment Provider Balance ₦3,500 / Cr the Codex bank account's ledger account ₦3,500.",
    "Nothing is booked in any school's books.",
  ],
}));

/* =====================================================================
 * PART 7. RECORD ONLY
 * ===================================================================== */
add(h1("Part 7. RECORD ONLY finance transactions"));
add(body("Nothing here touches Paystack. Each test records something in the books, and the journal tells you what a correct entry looks like. B1 to B6 share one test bill of ₦500 for Ada, so do them in order. Amounts are small on purpose. The accounts named are the starter chart; yours may show other names if the school changed them."));
const NOPAY = ["Nothing. If you see a Paystack entry, stop and note it."];

add(scenario({
  id: "B1", title: "Invoice issue", badge: "RECORD", branch: "Ikeja",
  who: "Ikeja bursar", money: "None", fees: "None",
  steps: [
    "Receivables, Customers & Billing, Invoices, New invoice.",
    "Customer Ada, one line: income account 4100 Operating Revenue, quantity 1, unit price ₦500. Keep Issue now ticked. Select Issue invoice.",
  ],
  app: ["The invoice reads Issued with a total and balance of ₦500, and a document number. It appears under Invoices, status Issued."],
  paystack: NOPAY,
  plain: ["Ada's family now owes ₦500, and the school has earned ₦500 of income."],
  journal: ["Ikeja: Dr 1200 Accounts receivable ₦500 / Cr 4100 Operating Revenue ₦500."],
}));

add(scenario({
  id: "B2", title: "Cash receipt recorded by hand", badge: "RECORD", branch: "Ikeja",
  who: "Ikeja bursar", money: "None (cash you hold)", fees: "None",
  steps: [
    "Receivables, Money Received, Receipts & Allocation, Record receipt.",
    "Customer Ada, the real date, Method Cash, Amount ₦100, Bank account: Ikeja's cash or bank account, Reference \"CASH-1\".",
    "In the allocation panel keep Auto-allocate on, oldest first, and apply.",
  ],
  app: ["The receipt is saved. B1's invoice reads Partly paid with a balance of ₦400."],
  paystack: NOPAY,
  plain: ["The school recorded ₦100 received and applied it to the bill."],
  journal: ["Ikeja: Dr Cash & Bank ₦100 / Cr 1200 Accounts receivable ₦100."],
}));

add(scenario({
  id: "B3", title: "Bank transfer receipt recorded by hand", badge: "RECORD", branch: "Ikeja",
  who: "Ikeja bursar", money: "None", fees: "None",
  note: ["Record only money Paystack did not already confirm. Recording a confirmed online payment again credits the family twice."],
  steps: [
    "Open B1's invoice, Settlements tab, Record payment.",
    "Amount ₦100, Method Bank transfer, Deposit account: Ikeja's bank account, Reference: the bank's transfer number.",
    "Save.",
  ],
  app: ["The invoice balance falls to ₦300. The Settlements tab lists a Payment of ₦100 by Bank transfer."],
  paystack: NOPAY,
  plain: ["Same as B2, by bank transfer. Match the bank line later in bank reconciliation."],
  journal: ["Ikeja: Dr Cash & Bank (Ikeja's bank ledger account) ₦100 / Cr 1200 Accounts receivable ₦100."],
}));

add(scenario({
  id: "B4", title: "Credit note", badge: "RECORD", branch: "Ikeja",
  who: "Ikeja bursar", money: "None", fees: "None",
  note: ["Credit notes and concessions need a second person only from ₦50,000 up (the school's setting). Both amounts here post at once."],
  steps: [
    "Receivables, Adjustments, Credit / Debit Notes, create a note.",
    "Choose Credit note, today's date, customer Ada, income account 4900 Sales Returns & Allowances, Against invoice: B1's invoice, amount ₦50, a reason.",
    "Apply on issue: apply to the oldest invoice. Save.",
  ],
  app: ["The note reads Applied. B1's invoice balance falls to ₦250."],
  paystack: NOPAY,
  plain: ["The school agreed the bill was ₦50 too high and lowered it."],
  journal: ["Ikeja: Dr 4900 Sales Returns & Allowances ₦50 / Cr 1200 Accounts receivable ₦50."],
}));

add(scenario({
  id: "B5", title: "Write-off", badge: "RECORD", branch: "Ikeja",
  who: "Ikeja bursar; your second person approves", money: "None", fees: "None",
  note: ["A write-off clears a debt the school has given up on. It cannot be voided, so use a small amount. A write-off always needs a second person's approval.", "The form shows the school's own bad-debts account as the default (5350 in the starter chart)."],
  steps: [
    "Receivables, Adjustments, Refunds & Write-offs, New action. Action: Write off to expense.",
    "Customer Ada, Against invoice: B1's invoice. Amount ₦100 (the form may start at the whole balance, so change it). Reason \"Test write-off\". Leave the expense account on its default.",
    "Next action: Submit for approval. Your second person approves.",
  ],
  app: ["The write-off posts after approval. The invoice balance falls by ₦100. The Written off (YTD) tile on Refunds & Write-offs rises."],
  paystack: NOPAY,
  plain: ["The school accepted it will never collect ₦100 and recorded it as a bad-debt cost."],
  journal: ["Ikeja: Dr 5350 Bad Debts ₦100 / Cr 1200 Accounts receivable ₦100."],
}));

add(scenario({
  id: "B6", title: "Concession", badge: "RECORD", branch: "Ikeja",
  who: "Ikeja bursar", money: "None", fees: "None",
  steps: [
    "Receivables, Adjustments, Concessions, create a concession.",
    "Customer Ada, Against invoice: B1's invoice, type Discount, Enter as Amount ₦50, Basis \"Test sibling discount\". Select Post concession.",
  ],
  app: ["The concession reads Posted. The invoice balance falls to ₦100. The tile Posted (YTD) rises."],
  paystack: NOPAY,
  plain: ["A discount the school agreed to give, shown separately from the income it reduced."],
  journal: ["Ikeja: Dr 4910 Discounts & Concessions ₦50 / Cr 1200 Accounts receivable ₦50."],
}));

add(scenario({
  id: "B7", title: "Refundable deposit", badge: "RECORD", branch: "Ikeja",
  who: "Ikeja bursar", money: "None", fees: "None",
  steps: [
    "Receivables, Customers & Billing, Invoices, New invoice. Customer Ada, one line ₦200 with Refundable deposit ticked. Issue invoice.",
    "Receivables, Money Received, Deposits. Find Ada's deposit.",
  ],
  app: ["The deposit is listed with status Held and a link to the invoice. Return deposits is for a customer who has left, so leave it."],
  paystack: NOPAY,
  plain: ["A deposit is the family's money, not the school's income. It is held until it is returned or forfeited."],
  journal: ["Ikeja: Dr 1200 Accounts receivable ₦200 / Cr 2170 Customer Deposits Held ₦200."],
}));

add(scenario({
  id: "B8", title: "Petty cash: float, voucher and replenishment", badge: "RECORD", branch: "Ikeja",
  who: "Ikeja bursar", money: "None (imagine the cash tin)", fees: "None",
  steps: [
    "Operations, Spending, Petty Cash. Create a float: name \"Ikeja front desk\", petty-cash account 1110, a custodian, float ceiling ₦1,000, opening cash from Ikeja's bank, today's date.",
    "Add a voucher: expense account 5300 General & Administrative, ₦150, a note. Select Save & post.",
    "Select Replenish. The amount starts at the shortfall (₦150). Choose Ikeja's bank account and the date, then select Move.",
  ],
  app: [
    "The cards show Float ceiling ₦1,000, Current balance falling to ₦850 after the voucher, then back to ₦1,000 after Replenish.",
    "The Movement register lists all three movements.",
  ],
  paystack: NOPAY,
  plain: ["Cash moved from the bank into the tin, was spent, and was topped up again."],
  journal: [
    "Opening: Dr 1110 Petty Cash ₦1,000 / Cr Cash & Bank ₦1,000. Voucher: Dr 5300 ₦150 / Cr 1110 ₦150. Replenish: Dr 1110 ₦150 / Cr Cash & Bank ₦150.",
  ],
}));

add(scenario({
  id: "B9", title: "Inter-branch transfer", badge: "RECORD", branch: "Ikeja sends, Lekki receives",
  who: "Ikeja bursar sends; Lekki bursar confirms", money: "None to Paystack. Real money only if you also move it between your accounts", fees: "None",
  note: ["The menu Between Branches appears only at a school with more than one branch. A send follows the branch's approval route; if one is set, your second person approves."],
  steps: [
    "Between Branches, Money Movement, Inter-branch Transfers, Send money.",
    "Choose Ikeja's account, Lekki as the other branch, Amount ₦300, a purpose. Submit.",
    "As Lekki bursar, open the transfer and select Confirm it arrived.",
    "Between Branches, Balances & Shared Costs, Inter-branch Balances.",
  ],
  app: [
    "The transfer reads Sent, then Confirmed.",
    "Inter-branch Balances shows Lekki owing Ikeja ₦300, and whether both branches' books agree.",
  ],
  paystack: NOPAY,
  plain: ["Each branch keeps its own books and books its own side. Ikeja is owed ₦300 by Lekki until it is repaid, and the two 1260 lines net to zero across the branches."],
  journal: [
    "Ikeja: Dr 1260 Inter-branch Balances ₦300 / Cr Ikeja's bank ₦300.",
    "Lekki: Dr Lekki's bank ₦300 / Cr 1260 Inter-branch Balances ₦300.",
  ],
}));

add(scenario({
  id: "B10", title: "Expense claim", badge: "RECORD", branch: "Ikeja",
  who: "Ikeja bursar raises it; your second person approves", money: "None to Paystack", fees: "None",
  steps: [
    "Operations, Spending, Expense Claims, new claim. Claimant (a staff member), date, purpose \"Test materials\".",
    "One item: description, expense account 5300, Amount ₦200. Drag a receipt file on. Select Create and submit.",
    "Your second person approves the claim.",
    "Open the claim, choose the bank account and date, keep Amount to pay at ₦200, and select Pay.",
  ],
  app: ["The claim reads Awaiting approval, then Approved, then Paid. Paying less than the balance would leave it Part-paid."],
  paystack: NOPAY,
  plain: ["The school owed the claimant ₦200 when approved and paid it from the bank."],
  journal: ["On approval: Dr 5300 ₦200 / Cr 2400 Accrued Reimbursements ₦200. On paying: Dr 2400 ₦200 / Cr Cash & Bank ₦200."],
}));

add(scenario({
  id: "B11", title: "Vendor bills, and a vendor payment made outside the app", badge: "RECORD", branch: "Ikeja",
  who: "Ikeja bursar; your second person approves", money: "None to Paystack", fees: "None",
  note: ["Do this before O1. Record three bills for Test Vendor Ltd at Ikeja: ₦500 and ₦1,000 (left open for the payouts O1 and O2) and ₦300 (paid here by hand). Direct invoices only work if the school allows bills without a purchase order."],
  steps: [
    "Procurement, Vendor Invoices, Record Invoice, Direct invoice. Vendor Test Vendor Ltd, branch Ikeja, its own invoice number, dates, amount ₦300, an expense account. Submit for Approval. After approval, select Post Invoice. Repeat for ₦500 and ₦1,000.",
    "Vendor Payments, New Payment. Vendor, Method Bank transfer, Payment date, Pay from: Ikeja's bank account, Reference: a bank transfer number you invent for the test.",
    "Under Outstanding invoices settle the ₦300 bill. Select Create & Submit. After approval select Post Payment.",
  ],
  app: [
    "Each bill reads Posted. After the payment the ₦300 bill reads Paid and the others stay open.",
    "The payment screen shows Gross settled, Withholding tax kept back (nil unless the vendor has a tax code) and Net cash paid.",
  ],
  paystack: NOPAY,
  plain: ["The school recorded that it paid ₦300 from its own bank. Nothing was sent by Paystack."],
  journal: [
    "Each bill: Dr 5300 (the expense account you chose) / Cr 2100 Accounts payable.",
    "Payment: Dr 2100 Accounts payable ₦300 / Cr Cash & Bank ₦300 (and Cr 2300 WHT payable if the vendor has a tax code).",
  ],
}));

add(scenario({
  id: "B12", title: "Tax payment (the withholding tax from O2)", badge: "RECORD", branch: "Ikeja's share",
  who: "School administrator (covers the whole school)", money: "None to Paystack", fees: "None",
  note: ["Preparing, filing and paying are open only to someone who covers the whole school. Do this after O2."],
  steps: [
    "Operations, Planning & Compliance, Tax Remittance. Use the Withholding Tax obligation. Select Prepare filing for the period, with a due date.",
    "Open the return. It lists each branch's share: Ikeja ₦50. Enter the Filed date and a Filing reference.",
    "Select Pay. Choose the share, Pay from: Ikeja's bank account, the date and the amount (₦50).",
  ],
  app: ["The return reads Open, then Filed, then Paid. Ikeja's share shows what has been paid and what remains."],
  paystack: NOPAY,
  plain: ["The ₦50 kept back from the vendor in O2 was handed to the tax office."],
  journal: ["Ikeja: Dr 2300 WHT payable ₦50 / Cr Cash & Bank ₦50."],
}));

add(scenario({
  id: "B13", title: "Manual journal", badge: "RECORD", branch: "Ikeja",
  who: "Ikeja bursar (a school with a journal approval route needs your second person)", money: "None", fees: "None",
  steps: [
    "Ledger & Setup, Books, General Ledger, and use the button on that screen to create a journal.",
    "Date today, Reference \"JNL-1\", Narration \"Test correction of a bank charge\".",
    "Two lines: Debit 5500 Bank Charges ₦100, Credit 1100 Cash & Bank ₦100. The bar reads Balanced. Post it (or submit it if the school asks for approval).",
  ],
  app: ["The journal reads Posted, and the Difference card reads Balanced. Reverse on the posted journal would undo it."],
  paystack: NOPAY,
  plain: ["A correction no other screen can make."],
  journal: ["Ikeja: Dr 5500 Bank Charges ₦100 / Cr 1100 Cash & Bank ₦100."],
}));

add(scenario({
  id: "B14", title: "Month close for a branch", badge: "RECORD", branch: "Lekki (try one branch)",
  who: "Lekki bursar", money: "None", fees: "None",
  note: [
    "Do this last. Closing a month stops new entries dated in that month at that branch until it is re-opened.",
    "A month closes only when every earlier month is closed. If the school's year started earlier than this month, close each earlier month first (the checklist names the month that blocks), or ask the administrator to start the school's year this month.",
  ],
  steps: [
    "Ledger & Setup, Books, Fiscal Periods. Choose Lekki. Open the month.",
    "Read the Close checklist. A line marked Blocks the close must pass. Warning lines do not stop it.",
    "Select Soft close, then Run close steps and confirm Run period close.",
    "To undo: select Re-open and give a reason.",
  ],
  app: [
    "The month reads Closed for Lekki. Ikeja's month is unchanged. The school's month reads Closed only when every branch has closed it.",
    "Gateway clearing is checked. It warns if a payment has waited in clearing longer than the school's warning days.",
  ],
  paystack: NOPAY,
  plain: ["Lekki has finished the month. Late entries dated in it are refused."],
  journal: ["Close steps post any depreciation or fees billed ahead that fall due; there may be none."],
}));

/* =====================================================================
 * PART 8. FROM 1 NOVEMBER 2026
 * ===================================================================== */
add(h1("Part 8. From 1 November 2026 onward: direct custody"));
add(body("In direct custody each branch's online money goes straight to its own bank through a Paystack subaccount, and Codex holds nothing. The switch can only start on the 1st of a month, so these tests wait for 1 November."));
add(box("What changes when a school is paid direct", [
  "Codex's held money, its settlements and the Codex approvers are no longer involved. Paystack pays each branch's bank on its own schedule (usually the next working day).",
  "Paystack's fee is borne by the branch. It is booked when the school matches the bank line to the payments (D6), not when the parent pays.",
  "The school cannot refund or pay suppliers through Paystack any more. It pays from its bank and records the payment (Part 7).",
  "Before the switch: the payroll route must be \"From the school's bank\" (O11), every branch must be set up with Paystack, and the switch is refused if either is not true.",
], C.pale, C.accent));

add(scenario({
  id: "D1", title: "Switch to direct custody: final settlement first", badge: "LIVE", branch: "Whole school",
  who: "School administrator; Codex operator; two Codex approvers", money: "None (the held money is paid to your accounts first)", fees: "About ₦10 per final settlement transfer, borne by the branch",
  note: [
    "A change never starts at once: it starts on the first day of next month, and only after nothing is held. You can ask for it any time in October.",
    "Choosing the mode already in force cancels a change that is waiting.",
    "Parents who still pay into an old retired account number are credited to their customer, and the next settlement passes the money on.",
  ],
  steps: [
    "In October: confirm O11 is done (route From the school's bank), O12 settled everything, and every branch reads Set up with Paystack.",
    "Finance Settings, Banking and cash, Online payments. Under Who holds online payments choose **Straight to each branch's bank**. Select Save online payments and confirm \"Change who holds online payments?\".",
    "On 1 November at about 00:20 the app builds a final settlement for every branch, ignoring the interval. The Codex operator puts each forward and two Codex approvers approve (as O12).",
    "When every settlement is Paid and nothing is held, the mode changes by itself. Read the panel.",
    "Payments, Money In, Virtual Accounts: check Dayo's account.",
  ],
  app: [
    "Before the first of the month the panel shows the change as waiting, with a note naming what it waits for.",
    "After: the panel shows the mode in force as direct. Payouts and Batches leave the menu.",
    "Dayo's old account reads Retired and a new one, against the branch's subaccount, reads Active.",
  ],
  paystack: ["Transfers: the final settlement transfers. Customers: Dayo has a new dedicated account."],
  plain: ["Codex paid the school every naira it held, then stopped holding money for it."],
  journal: [
    "Final settlements: as O12.",
    "After the switch nothing more is added to Codex's held figures for this school.",
  ],
}));

add(scenario({
  id: "D2", title: "A checkout payment lands in the branch's bank", badge: "LIVE", branch: "Ikeja",
  who: "You as bursar, then as parent", money: "₦400 on a card", fees: "About 1.5% (about ₦6), borne by the branch",
  note: ["The app's request names the branch's subaccount and asks it to bear the fee. Whether Paystack honours that on every channel has not been confirmed."],
  steps: [
    "Raise a ₦400 bill for Ada and issue it.",
    "Request payment, Generate payment link, Copy. Pay by card in a private window (as M1).",
    "Look at Collections and the Online payments panel.",
    "Next working day: look at your bank X and the Paystack Settlements page.",
  ],
  app: [
    "Collections: the row reads Paid. The bill reads Paid.",
    "Ikeja's held amount does NOT go up, because Codex holds nothing now.",
  ],
  paystack: [
    "Transactions: the ₦400 payment shows the branch's subaccount and the fee borne by it.",
    "Settlements: the ₦400 less the fee is paid to account X on Paystack's schedule.",
  ],
  plain: ["The parent's bill is paid at once. The money is still \"in the post\" to the bank until D6."],
  journal: ["Ikeja: Dr 1125 Gateway clearing ₦400 / Cr 1200 Accounts receivable ₦400. Nothing in Codex's books."],
}));

add(scenario({
  id: "D3", title: "Pupil's account money lands in the branch's bank", badge: "LIVE", branch: "Ikeja",
  who: "You", money: "₦300 sent from your bank app", fees: "Check your Paystack pricing page for dedicated accounts",
  note: ["Whether Paystack honours the fee bearer on a dedicated account has not been confirmed."],
  steps: [
    "Open Dayo's new account (D1) and note its number.",
    "From your bank app send ₦300 to it.",
    "When Collections shows it Paid, allocate the credit to a bill if one is open (as M5).",
  ],
  app: ["Collections: a Virtual account transfer row, Paid, ₦300. The receipt waits as customer credit until allocated."],
  paystack: ["Transactions: the transfer through the dedicated account, tied to the subaccount."],
  plain: ["Same as M5, but the money goes to the branch's bank on Paystack's schedule, not to Codex."],
  journal: ["Ikeja: Dr 1125 Gateway clearing ₦300 / Cr 2140 Customer Credit ₦300. Nothing in Codex's books."],
}));

add(scenario({
  id: "D4", title: "Terminal payment routed through a split", badge: "LIVE", branch: "Ikeja",
  who: "You as parent; then Ikeja bursar", money: "₦300 on a card", fees: "Check your Paystack pricing page for terminals",
  note: ["The terminal routes through a Paystack split that sends the payment to the branch's subaccount. Until the split is attached, a payment just lands in Codex's balance. Whether the split carries the fee bearer the way a checkout does has not been confirmed."],
  steps: [
    "Payments, Money In, Branch Terminals. Ikeja's card now reads \"Paid straight into the branch's bank.\"",
    "Pay ₦300 on the terminal with Gbenga's billing email (give Gbenga a ₦300 bill first).",
    "Terminal Payments, Matched.",
  ],
  app: ["The payment is booked to Ikeja and matched to Gbenga as in M7. Ikeja's held amount does not rise."],
  paystack: ["Terminals and Transactions: the payment shows a split to the branch's subaccount."],
  plain: ["Same as M7, but the money goes to the branch's bank."],
  journal: [
    "On arrival, Ikeja: Dr 1125 Gateway clearing ₦300 / Cr 2195 Unidentified Receipts ₦300; on matching: Dr 2195 / Cr 1200. Nothing in Codex's books.",
  ],
}));

add(scenario({
  id: "D5", title: "Refunds and payouts are not offered online", badge: "RECORD", branch: "Whole school",
  who: "Any bursar; the developer for the last check", money: "None", fees: "None",
  steps: [
    "Receivables, Adjustments, Refunds & Write-offs, New action. Look for the choice Pay the refund.",
    "Payments, Money Out: look for Payouts and Batches in the menu. Open the address of Payouts by hand.",
    "Finance Settings, Payroll, How staff are paid: look at Through Paystack.",
    "Developer check: an online refund and an online payout through the API are refused with ONLINE_REFUNDS_NOT_OFFERED and ONLINE_PAYOUTS_NOT_OFFERED (409).",
  ],
  app: [
    "No \"Back through Paystack\" choice appears. A refund is paid from the bank and recorded.",
    "Payouts and Batches are absent from the menu. At the address you see: \"Online payouts are not available while online payments go straight to each branch's bank. Pay suppliers from the bank and record the payment.\"",
    "Through Paystack is closed, with the reason beside it.",
    "A chargeback on a direct school is recorded and raised to people. Nothing is booked.",
  ],
  paystack: ["Transfers and Refunds: nothing new."],
  plain: ["The money is already in the branch's bank, so a Paystack refund would come out of Codex's own balance. The app does not allow it."],
  journal: ["None."],
}));

add(scenario({
  id: "D6", title: "Paystack's fee is booked when the bank line is matched", badge: "RECORD", branch: "Ikeja",
  who: "Ikeja bursar (needs the permission to book settlements)", money: "None", fees: "The fee comes out of the settlement",
  note: ["Book settlement stays greyed out if the figures do not add up: the bank line brings more than the payments picked, the provider's fees do not match the shortfall, or the line is dated before a payment was received. The dialog says which."],
  steps: [
    "Import or enter the bank statement for account X (Operations, Banking, Bank Reconciliation) once Paystack has paid it.",
    "Payments, Settlement, To book tab. Find the suggested settlement for D2 to D4's payments and select Book.",
    "Tick the payments the line settles, check the journal, and select Book settlement.",
    "To undo: unmatch the bank line in Bank Reconciliation. The payments wait in gateway clearing again.",
  ],
  app: [
    "The journal preview shows the bank going up by what arrived, Bank Charges by the fee, and Gateway clearing coming down by the payments.",
    "After booking, the bank line is reconciled and the payments read Matched on the Settlement screen.",
  ],
  paystack: ["Settlements: the amount paid to X and the payments it carries."],
  plain: ["The parent paid ₦400. The bank received a little less. The difference is Paystack's fee, now shown as a bank charge."],
  journal: ["Ikeja: Dr Cash & Bank (what arrived) / Dr 5500 Bank Charges (the fee) / Cr 1125 Gateway clearing (the payments, in full)."],
}));

/* =====================================================================
 * PART 9. LIMITS, UNCONFIRMED THINGS, RESULTS
 * ===================================================================== */
add(h1("Part 9. Known limits, what Paystack has not confirmed, and results"));
add(body("Nothing here is a reason to stop. These are the places where the app is known to be incomplete, or where the book could not confirm something before you tested it. Several of your tests answer them, so write what you see."));

add(h2("9.1 Things Paystack has not confirmed (your tests settle many of these)"));
add(simpleTable([2300, 5300, 2306], ["Question", "What the app assumes", "Settled by"], [
  ["Can a payment made by bank transfer into a dedicated account be refunded?", "The app lets you try. A failure returns the refund to a draft.", "R4"],
  ["What does Paystack send when a refund finishes, and when does a refund leave the balance?", "The app re-checks every refund with Paystack itself before booking, so the event shape does not decide the books.", "R1, R2"],
  ["Where does a terminal's payment page link, QR or poster come back?", "The Branch Terminals card shows what Paystack returns. If it returns none the card says so.", "M7, M10"],
  ["Can a terminal be opened with no WhatsApp number?", "The app waits for a number before opening one, so this does not arise.", "-"],
  ["Where does the sender's name appear on a terminal bank transfer?", "Read from the authorization part of the payment. If absent the sender shows blank.", "M8"],
  ["Which error codes does the account-name lookup return?", "Not found is shown as Could not check. A timeout or outage is shown as Not checked yet.", "O4, O5"],
  ["When does the Paystack balance drop for a transfer?", "The app reserves the money in its own books when it sends, whatever Paystack shows.", "O1, C1"],
  ["Has a settlement still Processing already left the balance?", "The daily check records a gap it fully explains as not measured.", "C1"],
  ["Is a dispute resolved as auto-accepted lost?", "Read as lost. Declined is read as won.", "R5"],
  ["Does the fee bearer on a dedicated account and on a terminal split work?", "Sent in the request. Not confirmed.", "D2 to D4"],
  ["Does GET /balance report available money only, or available plus pending?", "The app uses whatever Paystack reports as the balance. If it reports available only, Codex's own share can read low while fresh payments are still settling.", "C1, O13"],
]));

add(h2("9.2 Limits of the app, as built"));
add(simpleTable([3000, 6906], ["Limit", "What it means for you"], [
  ["Held payouts above the balance are refused, not queued", "Only payroll lines wait for funds. A vendor payout, or Codex's own payout above its share (O14), is marked Failed with a reason."],
  ["No notification for a failed payroll payout", "Watch the run's payment panel (O7, O10)."],
  ["No notification for a new unmatched terminal payment", "Open Terminal Payments yourself. The WhatsApp alert is Paystack's, not the app's."],
  ["A terminal payment cannot be matched to another branch's customer", "By design. The picker leaves them out and the server refuses."],
  ["A payout's tax comes only from the vendor's default tax code", "The New payout form has no tax box. Without a code no tax is taken (O2)."],
  ["The Settlement reconciliation report may show a tax-bearing payout as costing a fee equal to the tax", "It compares the whole payout, not the amount sent."],
  ["The transfer-fee table is the app's own", "It is not read back from Paystack. If Paystack changes its fees, the app's reservation can be a few naira out."],
  ["A payroll run posted before approvals existed cannot be paid through Paystack", "Record it as paid from the bank (O9)."],
  ["The second payroll run for the same staff in the same month is not allowed", "A staff member already paid for the month is left off a new run. Use a pay date in another month (O11)."],
  ["An early look at a payment that is still on Paystack's checkout can read Abandoned", "A later confirmation overturns it and books the payment (M11)."],
  ["Refunded is never shown for a part refund", "The payment keeps reading Paid. The refund list shows what has been returned."],
  ["Staff bank details reset only when saved through the app", "There is no other way in, so a changed account is always re-checked."],
]));

add(h2("9.3 What this book could not confirm"));
add(bullets([
  "The exact wording of some buttons and tabs in the Paystack dashboard. Use the names that appear on your dashboard.",
  "Which versions of the school app and the Codex console are live on the day. Block B asks for builds carrying FinPro v0.7.69 or later and backend main d8e24f7a or later.",
  "Whether Codex's own books show the Payouts screen when its entity is chosen (O13). If they do not, the developer raises the payout.",
  "Paystack's own limits on transfers to third-party accounts for your business type, and whether a list of allowed server addresses is required for transfers. If your dashboard asks for one, add the production server's.",
  "The real fee for bank-transfer checkout, dedicated accounts, terminals and disputes. Read your Paystack pricing page and write the figures in the Notes boxes.",
  "How long Paystack takes to return a refund and to settle a subaccount. Write the times you see.",
], { size: 19 }));
add(para("", { after: 120 }));

add(h2("9.4 Results summary"));
add(body("Copy each Result from the scenario tables. Fail means the screen or the books did not match what the table said, or money behaved unexpectedly."));
const resultRows = INDEX.map((r) => [r.id, r.title, BADGE[r.badge].text, "", "", ""]);
add(simpleTable([620, 3900, 1600, 1100, 1286, 1400], ["ID", "Scenario", "Badge", "Result", "Date", "Initials"], resultRows, { size: 16 }));
add(body("Sign-off. Overall result: [   ] Ready for real use   [   ] Fix first.   Name: ____________________   Date: ______________", { before: 120 }));

/* =====================================================================
 * BUILD
 * ===================================================================== */
const doc = new Document({
  creator: "Codex Techhub",
  title: `XVS Paystack Live Test Book v${VERSION}`,
  description: "Owner's live Paystack test book for XVS finance",
  styles: {
    default: { document: { run: { font: FONT, size: 20 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 34, bold: true, font: FONT, color: C.ink }, paragraph: { spacing: { before: 120, after: 160 }, outlineLevel: 0 } },
      { id: "FooterSmall", name: "Footer Small", basedOn: "Normal", run: { size: 16, font: FONT, color: C.mid }, paragraph: { spacing: { before: 0, after: 0 } } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 25, bold: true, font: FONT, color: C.accent }, paragraph: { spacing: { before: 200, after: 100 }, outlineLevel: 1 } },
    ],
  },
  numbering: { config: [
    { reference: "steps", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 360, hanging: 320 } }, run: { size: 19, bold: true, color: C.accent } } }] },
    { reference: "bul", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 300, hanging: 220 } } } }] },
  ] },
  sections: [{
    properties: {
      titlePage: true,
      page: { size: { width: PAGE_W, height: PAGE_H }, margin: { top: MARGIN + 150, right: MARGIN, bottom: MARGIN + 100, left: MARGIN } },
    },
    headers: {
      first: new Header({ children: [new Paragraph({ children: [] })] }),
      default: new Header({ children: [new Paragraph({
        alignment: AlignmentType.RIGHT,
        border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: C.line, space: 4 } },
        children: [new TextRun({ text: "XVS Paystack Live Test Book   |   Codex Test School", font: FONT, size: 16, color: C.mid })],
      })] }),
    },
    footers: {
      first: new Footer({ children: [new Paragraph({ children: [] })] }),
      default: new Footer({ children: [new Paragraph({
        style: "FooterSmall",
        tabStops: [{ type: TabStopType.RIGHT, position: CONTENT_W }],
        children: [
          new TextRun({ text: `Version ${VERSION}   |   14 October 2026   |   Owner's copy`, font: FONT, size: 16, color: C.mid }),
          new TextRun({ text: "\tPage ", font: FONT, size: 16, color: C.mid }),
          new TextRun({ children: [PageNumber.CURRENT], font: FONT, size: 16, color: C.mid }),
          new TextRun({ text: " of ", font: FONT, size: 16, color: C.mid }),
          new TextRun({ children: [PageNumber.TOTAL_PAGES], font: FONT, size: 16, color: C.mid }),
        ],
      })] }),
    },
    children: content,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(OUT, buf);
  const counts = {};
  INDEX.forEach((r) => { const k = r.id.replace(/\d+/g, ""); counts[k] = (counts[k] || 0) + 1; });
  console.log("Wrote", OUT, "scenarios:", INDEX.length, JSON.stringify(counts));
});
