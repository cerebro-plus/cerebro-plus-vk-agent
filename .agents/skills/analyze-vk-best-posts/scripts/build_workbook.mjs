import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const args = Object.fromEntries(
  process.argv.slice(2).reduce((pairs, value, index, values) => {
    if (value.startsWith("--")) pairs.push([value.slice(2), values[index + 1]]);
    return pairs;
  }, []),
);
if (!args.dataset || !args["run-dir"] || !args.output) {
  throw new Error("Required: --dataset, --run-dir, --output");
}
const dataset = JSON.parse(await fs.readFile(args.dataset, "utf8"));
const runDir = path.resolve(args["run-dir"]);
const output = path.resolve(args.output);
const typeOrder = new Map([["Прямой", 0], ["Косвенный", 1], ["За внимание", 2]]);
const bestPosts = dataset.posts
  .filter((item) => item.is_best)
  .sort(
    (a, b) =>
      (typeOrder.get(a.competitor_type) ?? 9) -
        (typeOrder.get(b.competitor_type) ?? 9) ||
      Number(b.er ?? -1) - Number(a.er ?? -1),
  );
const checkedPosts = [...dataset.posts].sort(
  (a, b) =>
    (typeOrder.get(a.competitor_type) ?? 9) -
      (typeOrder.get(b.competitor_type) ?? 9) ||
    b.timestamp - a.timestamp,
);
const unavailable = dataset.unavailable ?? [];
const bestSummaryEnd = Math.max(5, 4 + bestPosts.length);
const checkedSummaryEnd = Math.max(5, 4 + checkedPosts.length);
const workbook = Workbook.create();
const summary = workbook.worksheets.add("Сводка");
const best = workbook.worksheets.add("Лучшие посты");
const checked = workbook.worksheets.add("Проверенные посты");
const missing = workbook.worksheets.add("Недоступно");
const colors = {
  navy: "#17324D",
  teal: "#1F7A75",
  blue: "#EAF1F8",
  green: "#DFF2E5",
  gold: "#FFF5D8",
  white: "#FFFFFF",
  text: "#1D2935",
  line: "#D8E1E8",
};
const link = (url, label) =>
  `=HYPERLINK("${String(url).replaceAll('"', '""')}","${label}")`;
const title = (sheet, range, value) => {
  sheet.mergeCells(range);
  sheet.getRange(range).values = [[value]];
  sheet.getRange(range).format = {
    fill: colors.navy,
    font: { bold: true, color: colors.white, size: 17 },
    verticalAlignment: "center",
  };
  sheet.getRange(range).format.rowHeightPx = 40;
};
const header = (range) => {
  range.format = {
    fill: colors.teal,
    font: { bold: true, color: colors.white, size: 10 },
    wrapText: true,
    horizontalAlignment: "center",
    verticalAlignment: "center",
  };
  range.format.rowHeightPx = 44;
};
const body = (range) => {
  range.format = {
    font: { color: colors.text, size: 9 },
    wrapText: true,
    verticalAlignment: "top",
    borders: { insideHorizontal: { style: "thin", color: colors.line } },
  };
};
const widths = (sheet, specs, endRow) => {
  for (const [column, width] of Object.entries(specs)) {
    sheet.getRange(`${column}1:${column}${endRow}`).format.columnWidth = width;
  }
};

summary.showGridLines = false;
title(summary, "A1:H1", "Лучшие посты конкурентов VK · 7×24 часа");
summary.mergeCells("A2:H2");
summary.getRange("A2").values = [[
  `Начало: ${dataset.run_started_at} · окно: ${dataset.window.start} — ${dataset.window.end} · статус: ${dataset.status}`,
]];
summary.getRange("A2:H2").format = {
  fill: dataset.status === "confirmed" ? colors.green : colors.blue,
  font: { italic: true, color: "#627487", size: 10 },
  wrapText: true,
};
summary.getRange("A4:B4").values = [["Показатель", "Значение"]];
header(summary.getRange("A4:B4"));
summary.getRange("A5:A10").values = [
  ["Сообществ"],
  ["Проверено постов"],
  ["Лучших постов"],
  ["Постов без просмотров"],
  ["Подтверждённых клипов"],
  ["Средний валидный ER"],
];
summary.getRange("B5:B10").formulas = [
  [`=${dataset.communities.length}`],
  [`=COUNTA('Проверенные посты'!A5:A${checkedSummaryEnd})`],
  [`=COUNTA('Лучшие посты'!A5:A${bestSummaryEnd})`],
  [`=COUNTIF('Проверенные посты'!P5:P${checkedSummaryEnd},"ER не рассчитан")`],
  [`=COUNTIF('Проверенные посты'!G5:G${checkedSummaryEnd},"Клип")`],
  [`=IFERROR(AVERAGE('Проверенные посты'!N5:N${checkedSummaryEnd}),0)`],
];
summary.getRange("B5:B9").format.numberFormat = "#,##0";
summary.getRange("B10").format.numberFormat = "0.00%";
summary.getRange("A13:H13").merge();
summary.getRange("A13").values = [["Методика"]];
summary.getRange("A13:H13").format = { fill: colors.gold, font: { bold: true } };
summary.getRange("A14:H18").merge(true);
summary.getRange("A14:A18").values = [
  ["ER = (лайки + комментарии + репосты) / просмотры."],
  ["Бенчмарк — среднее валидных ER сообщества в том же окне."],
  ["Лучший пост: ER не ниже индивидуального бенчмарка."],
  ["Пагинация стены и комментариев пройдена; дубли исключены."],
  ["Клипы подтверждены по внутреннему типу и расшифрованы Whisper small с VAD."],
];
body(summary.getRange("A14:H18"));
summary.freezePanes.freezeRows(2);
widths(summary, { A: 34, B: 18, C: 3, D: 20, E: 14, F: 14, G: 3, H: 3 }, 18);

title(best, "A1:N1", "Лучшие посты");
best.getRange("A4:N4").values = [[
  "Сообщество", "Тип конкурента", "Ссылка на пост", "Текст", "Тип поста",
  "Опрос", "Розыгрыш", "Лайки", "Комментарии", "ER поста", "Бенчмарк",
  "Расшифровка", "Изображения", "ID поста",
]];
header(best.getRange("A4:N4"));
if (bestPosts.length) {
  best.getRange(`A5:N${4 + bestPosts.length}`).values = bestPosts.map((post) => [
    post.community_name, post.competitor_type, null, post.text, post.post_type,
    post.poll ? "Да" : "Нет", post.giveaway ? "Да" : "Нет", post.likes,
    post.comments, post.er, post.benchmark,
    post.post_type === "Клип" ? post.media.transcript ?? "" : "",
    post.media.collage_path ?? "Нет изображений", String(post.post_id),
  ]);
  best.getRange(`C5:C${4 + bestPosts.length}`).formulas = bestPosts.map((post) => [
    link(post.url, "Открыть пост"),
  ]);
  body(best.getRange(`A5:N${4 + bestPosts.length}`));
  best.getRange(`J5:K${4 + bestPosts.length}`).format.numberFormat = "0.00%";
  for (let index = 0; index < bestPosts.length; index += 1) {
    const row = 4 + index;
    best.getRangeByIndexes(row, 0, 1, 14).format.rowHeightPx = 180;
    const collage = bestPosts[index].media.collage_path;
    if (!collage) continue;
    const bytes = await fs.readFile(path.join(runDir, collage));
    const sourceWidth = Number(bestPosts[index].media.collage_width || 220);
    const sourceHeight = Number(bestPosts[index].media.collage_height || 160);
    const imageScale = Math.min(220 / sourceWidth, 160 / sourceHeight);
    const renderedWidth = Math.max(1, Math.round(sourceWidth * imageScale));
    const renderedHeight = Math.max(1, Math.round(sourceHeight * imageScale));
    best.images.add({
      dataUrl: `data:image/jpeg;base64,${bytes.toString("base64")}`,
      anchor: {
        from: {
          row,
          col: 12,
          rowOffsetPx: 8 + Math.floor((160 - renderedHeight) / 2),
          colOffsetPx: 8 + Math.floor((220 - renderedWidth) / 2),
        },
        extent: { widthPx: renderedWidth, heightPx: renderedHeight },
      },
    });
  }
  best.tables.add(`A4:N${4 + bestPosts.length}`, true, "BestPostsTable").style =
    "TableStyleMedium2";
} else {
  best.tables.add("A4:N4", true, "BestPostsTable").style = "TableStyleMedium2";
}
best.freezePanes.freezeRows(4);
widths(best, { A: 27, B: 17, C: 16, D: 45, E: 15, F: 9, G: 11, H: 10, I: 12, J: 12, K: 12, L: 38, M: 34, N: 20 }, 4 + Math.max(1, bestPosts.length));

title(checked, "A1:Q1", "Проверенные посты");
checked.getRange("A4:Q4").values = [[
  "Сообщество", "VK сообщества", "Подписчики", "Дата поста", "Ссылка на пост",
  "Текст", "Тип поста", "Опрос", "Розыгрыш", "Лайки", "Комментарии",
  "Репосты", "Просмотры", "ER поста", "Бенчмарк", "Результат", "Статус данных",
]];
header(checked.getRange("A4:Q4"));
if (checkedPosts.length) {
  checked.getRange(`A5:Q${4 + checkedPosts.length}`).values = checkedPosts.map((post) => [
    post.community_name, null, post.followers, new Date(post.date), null, post.text,
    post.post_type, post.poll ? "Да" : "Нет", post.giveaway ? "Да" : "Нет",
    post.likes, post.comments, post.reposts, post.views, post.er, post.benchmark,
    post.result, post.data_status,
  ]);
  checked.getRange(`B5:B${4 + checkedPosts.length}`).formulas = checkedPosts.map((post) => [
    link(post.community_vk, "Открыть VK"),
  ]);
  checked.getRange(`E5:E${4 + checkedPosts.length}`).formulas = checkedPosts.map((post) => [
    link(post.url, "Открыть пост"),
  ]);
  body(checked.getRange(`A5:Q${4 + checkedPosts.length}`));
  checked.getRange(`D5:D${4 + checkedPosts.length}`).format.numberFormat = "dd.mm.yyyy hh:mm";
  checked.getRange(`N5:O${4 + checkedPosts.length}`).format.numberFormat = "0.00%";
  checked.getRange(`A5:Q${4 + checkedPosts.length}`).format.rowHeightPx = 70;
  checked.tables.add(
    `A4:Q${4 + checkedPosts.length}`,
    true,
    "CheckedPostsTable",
  ).style = "TableStyleMedium2";
} else {
  checked.tables.add("A4:Q4", true, "CheckedPostsTable").style =
    "TableStyleMedium2";
}
checked.freezePanes.freezeRows(4);
widths(checked, { A: 27, B: 15, C: 12, D: 18, E: 16, F: 48, G: 15, H: 9, I: 11, J: 10, K: 12, L: 10, M: 11, N: 12, O: 12, P: 18, Q: 18 }, 4 + Math.max(1, checkedPosts.length));

title(missing, "A1:C1", "Недоступно");
missing.getRange("A4:C4").values = [["Объект", "Ссылка", "Конкретная причина"]];
header(missing.getRange("A4:C4"));
const missingRows = unavailable.length
  ? unavailable
  : [{ object: "Нет", link: "", reason: "Недоступных объектов не выявлено." }];
missing.getRange(`A5:C${4 + missingRows.length}`).values = missingRows.map((item) => [
  item.object, item.link || "", item.reason,
]);
for (let index = 0; index < missingRows.length; index += 1) {
  if (!missingRows[index].link) continue;
  missing.getRange(`B${5 + index}`).formulas = [[
    link(missingRows[index].link, "Открыть"),
  ]];
}
body(missing.getRange(`A5:C${4 + missingRows.length}`));
missing.tables.add(`A4:C${4 + missingRows.length}`, true, "UnavailableTable").style =
  "TableStyleMedium2";
missing.freezePanes.freezeRows(4);
widths(missing, { A: 34, B: 18, C: 75 }, 4 + missingRows.length);

const formulaErrors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
});
await fs.mkdir(path.dirname(output), { recursive: true });
const file = await SpreadsheetFile.exportXlsx(workbook);
await file.save(output);
const audit = {
  sheets: workbook.worksheets.items.map((item) => item.name),
  best_columns: best.getRange("A4:N4").values[0],
  checked_columns: checked.getRange("A4:Q4").values[0],
  unavailable_columns: missing.getRange("A4:C4").values[0],
  best_rows: bestPosts.length,
  checked_rows: checkedPosts.length,
  embedded_images: bestPosts.filter((item) => item.media.collage_path).length,
  formula_errors: formulaErrors.ndjson,
};
process.stdout.write(JSON.stringify(audit));
process.exit(0);
