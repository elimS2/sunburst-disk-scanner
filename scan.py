#!/usr/bin/env python3
"""Dependency-free disk scanner with a basic HTML report."""

from __future__ import annotations

import argparse
import html
import json
import os
import stat as statmod
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_REPORT = PROJECT_DIR / "disk_report.html"
SMOKE_REPORT = PROJECT_DIR / "_smoke_report.html"

# Progressive browser loading: one full scan, three embedded JSON depth tiers.
TIER1_DEPTH = 5  # levels 1-5 from scan root — parsed on page load
TIER2_DEPTH = 5  # levels 6-10 — parsed in idle time after first paint
# Level 11+ — parsed on first drill into a deferred folder.


Node = dict[str, Any]


def human_size(size: int) -> str:
    """Return a compact size string using common binary-based units."""
    value = float(max(size, 0))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def safe_path_string(path: Path) -> str:
    try:
        return str(path.absolute())
    except OSError:
        return str(path)


def display_name(path: Path) -> str:
    return path.name or safe_path_string(path)


def error_text(exc: BaseException) -> str:
    message = str(exc)
    if message:
        return f"{exc.__class__.__name__}: {message}"
    return exc.__class__.__name__


def is_reparse_point(stat_result: os.stat_result) -> bool:
    attrs = getattr(stat_result, "st_file_attributes", 0)
    reparse_flag = getattr(statmod, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and attrs & reparse_flag)


def make_node(
    *,
    path: Path,
    node_type: str,
    size: int = 0,
    children: list[Node] | None = None,
    error: str | None = None,
) -> Node:
    node: Node = {
        "name": display_name(path),
        "path": safe_path_string(path),
        "type": node_type,
        "size": max(int(size), 0),
        "size_human": human_size(size),
        "children": children or [],
    }
    if error:
        node["error"] = error
    return node


def make_error_node(path: Path, node_type: str, exc: BaseException) -> Node:
    return make_node(path=path, node_type=node_type, error=error_text(exc))


def scan_entry(entry: os.DirEntry[str]) -> Node:
    path = Path(entry.path)
    try:
        stat_result = entry.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        return make_error_node(path, "missing", exc)
    except PermissionError as exc:
        return make_error_node(path, "error", exc)
    except OSError as exc:
        return make_error_node(path, "error", exc)

    if entry.is_symlink() or is_reparse_point(stat_result):
        return make_node(path=path, node_type="link", size=stat_result.st_size)

    mode = stat_result.st_mode
    if statmod.S_ISDIR(mode):
        return scan_directory(path)
    if statmod.S_ISREG(mode):
        return make_node(path=path, node_type="file", size=stat_result.st_size)
    return make_node(path=path, node_type="other", size=stat_result.st_size)


def sort_children(children: list[Node]) -> list[Node]:
    return sorted(children, key=lambda node: (-int(node["size"]), node["name"].lower()))


def scan_directory(path: Path) -> Node:
    children: list[Node] = []
    scan_error: str | None = None

    try:
        with os.scandir(path) as entries:
            for entry in entries:
                children.append(scan_entry(entry))
    except FileNotFoundError as exc:
        return make_error_node(path, "missing", exc)
    except PermissionError as exc:
        scan_error = error_text(exc)
    except OSError as exc:
        scan_error = error_text(exc)

    children = sort_children(children)
    total_size = sum(int(child["size"]) for child in children)
    return make_node(
        path=path,
        node_type="directory",
        size=total_size,
        children=children,
        error=scan_error,
    )


def scan_path(path: Path) -> Node:
    try:
        stat_result = path.lstat()
    except FileNotFoundError as exc:
        return make_error_node(path, "missing", exc)
    except PermissionError as exc:
        return make_error_node(path, "error", exc)
    except OSError as exc:
        return make_error_node(path, "error", exc)

    if path.is_symlink() or is_reparse_point(stat_result):
        return make_node(path=path, node_type="link", size=stat_result.st_size)

    mode = stat_result.st_mode
    if statmod.S_ISDIR(mode):
        return scan_directory(path)
    if statmod.S_ISREG(mode):
        return make_node(path=path, node_type="file", size=stat_result.st_size)
    return make_node(path=path, node_type="other", size=stat_result.st_size)


def limit_depth(node: Node, max_depth: int, depth: int = 0) -> Node:
    """Return a copy of *node* with children truncated at *max_depth* (0-based depth)."""
    children = list(node.get("children") or [])
    result: Node = {
        "name": node["name"],
        "path": node["path"],
        "type": node["type"],
        "size": node["size"],
        "size_human": node["size_human"],
        "children": [],
    }
    if node.get("error"):
        result["error"] = node["error"]

    if node.get("type") != "directory" or not children:
        return result

    if depth >= max_depth:
        if children:
            result["deferred"] = True
        return result

    result["children"] = [
        limit_depth(child, max_depth, depth + 1) for child in children
    ]
    return result


def index_nodes_by_path(root: Node) -> dict[str, Node]:
    """Build a path -> node lookup in a single tree walk."""
    index: dict[str, Node] = {str(root["path"]): root}

    def walk(node: Node) -> None:
        for child in node.get("children") or []:
            index[str(child["path"])] = child
            walk(child)

    walk(root)
    return index


def collect_deferred_paths(node: Node) -> list[str]:
    """Collect paths of folder nodes that still have unloaded deeper children."""
    paths: list[str] = []
    if node.get("deferred"):
        paths.append(str(node["path"]))
    for child in node.get("children") or []:
        paths.extend(collect_deferred_paths(child))
    return paths


def expansion_slice(node: Node) -> Node:
    """Minimal node payload used when merging a deeper tier."""
    return {
        "name": node["name"],
        "path": node["path"],
        "type": node["type"],
        "size": node["size"],
        "size_human": node["size_human"],
        "children": list(node.get("children") or []),
    }


def prepare_tiered_payload(
    full: Node,
    *,
    tier1_depth: int = TIER1_DEPTH,
    tier2_depth: int = TIER2_DEPTH,
) -> tuple[Node, Node, dict[str, Node]]:
    """Split a full scan tree into three progressively loaded depth tiers."""
    tier1 = limit_depth(full, tier1_depth, 0)
    tier2_tree = limit_depth(full, tier1_depth + tier2_depth, 0)
    path_index = index_nodes_by_path(full)
    tier3_map: dict[str, Node] = {}

    for path in collect_deferred_paths(tier2_tree):
        node = path_index.get(path)
        if node is not None:
            tier3_map[path] = expansion_slice(node)

    return tier1, tier2_tree, tier3_map


def json_payload_for_html(data: Any) -> str:
    """Serialize JSON so it is safe inside an HTML script tag."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def report_html(data: Node) -> str:
    tier1, tier2_tree, tier3_map = prepare_tiered_payload(data)
    meta_payload = json_payload_for_html(
        {
            "tier1_depth": TIER1_DEPTH,
            "tier2_depth": TIER2_DEPTH,
            "tier3_paths": len(tier3_map),
            "root_size": data["size_human"],
        }
    )
    tier1_payload = json_payload_for_html(tier1)
    tier2_payload = json_payload_for_html(tier2_tree)
    tier3_payload = json_payload_for_html(tier3_map)
    title = html.escape(f"Disk Scan Report - {data['name']}")
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f8fafc;
      --card: #ffffff;
      --text: #111827;
      --muted: #64748b;
      --border: #e5e7eb;
      --accent: #2563eb;
      --accent-dark: #1d4ed8;
      --danger: #b91c1c;
    }
    body {
      margin: 0;
      padding: 2rem;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--bg);
    }
    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 1.5rem;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: 0 10px 30px rgba(15, 23, 42, 0.06);
    }
    h1 {
      margin-top: 0;
      margin-bottom: 0.25rem;
    }
    h2 {
      margin-top: 0;
      font-size: 1rem;
    }
    button {
      border: 1px solid var(--accent);
      border-radius: 8px;
      padding: 0.5rem 0.75rem;
      color: #ffffff;
      background: var(--accent);
      font: inherit;
      cursor: pointer;
    }
    button:hover:not(:disabled), button:focus-visible:not(:disabled) {
      background: var(--accent-dark);
    }
    button:disabled {
      border-color: #cbd5e1;
      color: #94a3b8;
      background: #f1f5f9;
      cursor: not-allowed;
    }
    .muted {
      color: var(--muted);
    }
    .error {
      color: var(--danger);
      font-weight: 600;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      align-items: center;
      margin: 1.25rem 0;
    }
    .breadcrumb {
      min-width: 0;
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(320px, 720px) minmax(260px, 1fr);
      gap: 1.5rem;
      align-items: start;
    }
    .chart-card, .details-card {
      border: 1px solid var(--border);
      border-radius: 12px;
      background: #ffffff;
    }
    .chart-card {
      position: relative;
      padding: 1rem;
    }
    #sunburst {
      display: block;
      width: 100%;
      height: auto;
      max-width: 720px;
      margin: 0 auto;
    }
    .segment {
      stroke: #ffffff;
      stroke-width: 1.5;
      outline: none;
      transition: opacity 120ms ease, filter 120ms ease;
    }
    .segment:hover, .segment:focus {
      opacity: 0.88;
      filter: drop-shadow(0 2px 3px rgba(15, 23, 42, 0.22));
    }
    .segment.folder {
      cursor: pointer;
    }
    .segment.deferred {
      stroke-dasharray: 4 3;
    }
    .center-disc {
      fill: #ffffff;
      stroke: #cbd5e1;
      stroke-width: 2;
    }
    .center-title {
      font-size: 17px;
      font-weight: 700;
      fill: var(--text);
    }
    .center-meta {
      font-size: 13px;
      fill: var(--muted);
    }
    .empty-note {
      fill: var(--muted);
      font-size: 15px;
    }
    .details-card {
      padding: 1rem;
    }
    .details-grid {
      display: grid;
      grid-template-columns: max-content minmax(0, 1fr);
      gap: 0.4rem 0.75rem;
      margin: 0;
    }
    .details-grid dt {
      color: var(--muted);
    }
    .details-grid dd {
      margin: 0;
      overflow-wrap: anywhere;
    }
    .children {
      margin: 1rem 0 0;
      padding: 0;
      list-style: none;
      max-height: 22rem;
      overflow: auto;
      border-top: 1px solid var(--border);
    }
    .children li {
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      padding: 0.55rem 0;
      border-bottom: 1px solid var(--border);
    }
    .child-name {
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .child-size {
      flex: none;
      color: var(--muted);
    }
    .tooltip {
      position: fixed;
      z-index: 10;
      max-width: min(28rem, calc(100vw - 2rem));
      padding: 0.65rem 0.75rem;
      border: 1px solid #cbd5e1;
      border-radius: 8px;
      color: var(--text);
      background: rgba(255, 255, 255, 0.98);
      box-shadow: 0 12px 32px rgba(15, 23, 42, 0.18);
      pointer-events: none;
      overflow-wrap: anywhere;
      font-size: 0.9rem;
    }
    .tooltip div + div {
      margin-top: 0.2rem;
    }
    .tooltip-label {
      color: var(--muted);
    }
    details {
      margin-top: 1.5rem;
    }
    pre {
      overflow: auto;
      padding: 1rem;
      background: #111827;
      color: #e5e7eb;
      border-radius: 8px;
    }
    @media (max-width: 880px) {
      body {
        padding: 0.75rem;
      }
      main {
        padding: 1rem;
      }
      .layout {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main>
    <h1>Disk Scan Report</h1>
    <p class="muted">Self-contained local SVG sunburst report. Folder sectors can be clicked to drill in. Large scans load depth tiers progressively.</p>
    <div class="toolbar" aria-label="Navigation controls">
      <button id="back-button" type="button">Back</button>
      <button id="root-button" type="button">Root</button>
      <span id="breadcrumb" class="breadcrumb"></span>
      <span id="load-status" class="breadcrumb"></span>
    </div>
    <div class="layout">
      <section class="chart-card" aria-label="Sunburst disk usage chart">
        <svg id="sunburst" viewBox="0 0 720 720" role="img" aria-labelledby="chart-title chart-desc"></svg>
        <div id="tooltip" class="tooltip" role="status" hidden></div>
      </section>
      <aside class="details-card" aria-label="Selected node details">
        <h2>Selected folder</h2>
        <dl id="selected-details" class="details-grid"></dl>
        <p id="empty-state" class="muted"></p>
        <h2>Children</h2>
        <ul id="children-list" class="children"></ul>
      </aside>
    </div>
    <details>
      <summary>Load status</summary>
      <pre id="json-view"></pre>
    </details>
  </main>
  <script id="scan-meta" type="application/json">__META__</script>
  <script id="scan-tier-1" type="application/json">__TIER1__</script>
  <script id="scan-tier-2" type="application/json">__TIER2__</script>
  <script id="scan-tier-3" type="application/json">__TIER3__</script>
  <script>
    const meta = JSON.parse(document.getElementById("scan-meta").textContent);
    const data = JSON.parse(document.getElementById("scan-tier-1").textContent);
    const svg = document.getElementById("sunburst");
    const tooltip = document.getElementById("tooltip");
    const details = document.getElementById("selected-details");
    const childrenList = document.getElementById("children-list");
    const emptyState = document.getElementById("empty-state");
    const breadcrumb = document.getElementById("breadcrumb");
    const loadStatus = document.getElementById("load-status");
    const backButton = document.getElementById("back-button");
    const rootButton = document.getElementById("root-button");
    const jsonView = document.getElementById("json-view");

    let tier2Tree = null;
    let tier3Map = null;
    let tier2Loaded = false;
    let tier3Loaded = false;

    const SVG_NS = "http://www.w3.org/2000/svg";
    const SIZE = 720;
    const CENTER = SIZE / 2;
    const TAU = Math.PI * 2;
    const CENTER_RADIUS = 74;
    const RING_WIDTH = 68;
    const RING_GAP = 1.4;
    const MAX_DEPTH = 5;
    const PALETTE = [
      "#2563eb", "#16a34a", "#ea580c", "#9333ea", "#0891b2", "#dc2626",
      "#4f46e5", "#65a30d", "#d97706", "#be123c", "#0d9488", "#7c3aed"
    ];
    let currentNode = data;
    let history = [];

    function updateLoadStatus() {
      const tier2Label = tier2Loaded ? "loaded" : "pending";
      const tier3Label = tier3Loaded ? "loaded" : (meta.tier3_paths ? "pending" : "n/a");
      loadStatus.textContent = `Tiers: 1-5 ready | 6-10: ${tier2Label} | 11+: ${tier3Label}`;
      jsonView.textContent = [
        `Root size: ${meta.root_size}`,
        `Tier 1 depth: levels 1-${meta.tier1_depth}`,
        `Tier 2 depth: levels 6-${meta.tier1_depth + meta.tier2_depth}`,
        `Tier 3 branches: ${meta.tier3_paths}`,
        `Tier 2 status: ${tier2Label}`,
        `Tier 3 status: ${tier3Label}`,
      ].join("\\n");
    }

    function parseTierScript(id) {
      const element = document.getElementById(id);
      if (!element || !element.textContent.trim()) {
        return {};
      }
      return JSON.parse(element.textContent);
    }

    function loadTier2() {
      if (tier2Loaded) {
        return;
      }
      tier2Tree = parseTierScript("scan-tier-2");
      if (tier2Tree && tier2Tree.path === data.path) {
        patchDeferredFromTree(data, tier2Tree);
      }
      tier2Loaded = true;
      updateLoadStatus();
    }

    function findInTree(node, path) {
      if (node.path === path) {
        return node;
      }
      for (const child of childrenOf(node)) {
        const found = findInTree(child, path);
        if (found) {
          return found;
        }
      }
      return null;
    }

    function patchDeferredFromTree(target, source) {
      if (target.deferred) {
        const sourceNode = findInTree(source, target.path);
        if (sourceNode) {
          mergeExpansion(target, sourceNode);
        }
      }
      for (const child of childrenOf(target)) {
        patchDeferredFromTree(child, source);
      }
    }

    function loadTier3() {
      if (tier3Loaded) {
        return;
      }
      tier3Map = parseTierScript("scan-tier-3");
      tier3Loaded = true;
      updateLoadStatus();
    }

    function scheduleTierLoading() {
      const idle = window.requestIdleCallback || ((callback) => setTimeout(callback, 120));
      idle(() => {
        try {
          loadTier2();
        } catch (error) {
          console.warn("Tier 2 load failed", error);
        }
        idle(() => {
          try {
            loadTier3();
          } catch (error) {
            console.warn("Tier 3 load failed", error);
          }
        });
      });
    }

    function mergeExpansion(node, expansion) {
      node.children = Array.isArray(expansion.children) ? expansion.children : [];
      if (expansion.deferred) {
        node.deferred = true;
      } else {
        delete node.deferred;
      }
    }

    function resolveDeferred(node) {
      if (!node.deferred) {
        return;
      }
      if (!tier2Loaded) {
        loadTier2();
      }
      if (!node.deferred) {
        return;
      }
      if (!tier3Loaded) {
        loadTier3();
      }
      if (tier3Map && tier3Map[node.path]) {
        mergeExpansion(node, tier3Map[node.path]);
        delete node.deferred;
      }
    }

    function childrenOf(node) {
      return Array.isArray(node.children) ? node.children : [];
    }

    function nodeSize(node) {
      return Math.max(Number(node.size) || 0, 0);
    }

    function isFolder(node) {
      if (node.type !== "directory") {
        return false;
      }
      return childrenOf(node).length > 0 || Boolean(node.deferred);
    }

    function nodeLabel(node) {
      return `${node.name} (${node.type}, ${node.size_human})`;
    }

    function pathParts(root, target, trail = []) {
      const nextTrail = trail.concat(root);
      if (root === target) {
        return nextTrail;
      }
      for (const child of childrenOf(root)) {
        const result = pathParts(child, target, nextTrail);
        if (result) {
          return result;
        }
      }
      return null;
    }

    function polar(radius, angle) {
      return {
        x: CENTER + Math.cos(angle - Math.PI / 2) * radius,
        y: CENTER + Math.sin(angle - Math.PI / 2) * radius
      };
    }

    function arcPath(innerRadius, outerRadius, startAngle, endAngle) {
      const span = Math.max(endAngle - startAngle, 0);
      const end = span >= TAU ? startAngle + TAU - 0.0001 : endAngle;
      const largeArc = end - startAngle > Math.PI ? 1 : 0;
      const outerStart = polar(outerRadius, startAngle);
      const outerEnd = polar(outerRadius, end);
      const innerEnd = polar(innerRadius, end);
      const innerStart = polar(innerRadius, startAngle);
      return [
        `M ${outerStart.x.toFixed(3)} ${outerStart.y.toFixed(3)}`,
        `A ${outerRadius} ${outerRadius} 0 ${largeArc} 1 ${outerEnd.x.toFixed(3)} ${outerEnd.y.toFixed(3)}`,
        `L ${innerEnd.x.toFixed(3)} ${innerEnd.y.toFixed(3)}`,
        `A ${innerRadius} ${innerRadius} 0 ${largeArc} 0 ${innerStart.x.toFixed(3)} ${innerStart.y.toFixed(3)}`,
        "Z"
      ].join(" ");
    }

    function weightedSegments(parent, startAngle, endAngle) {
      const children = childrenOf(parent);
      if (!children.length) {
        return [];
      }
      const sizes = children.map(nodeSize);
      const total = sizes.reduce((sum, size) => sum + size, 0);
      const weights = total > 0 ? sizes : children.map(() => 1);
      const weightTotal = weights.reduce((sum, weight) => sum + weight, 0);
      if (weightTotal <= 0) {
        return [];
      }
      let cursor = startAngle;
      const span = endAngle - startAngle;
      return children.map((child, index) => {
        const childSpan = span * (weights[index] / weightTotal);
        const segment = {
          node: child,
          start: cursor,
          end: cursor + childSpan,
          zeroMode: total <= 0
        };
        cursor += childSpan;
        return segment;
      });
    }

    function colorFor(node, depth, index) {
      let hash = depth * 97 + index * 17;
      for (const char of String(node.path || node.name)) {
        hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
      }
      return PALETTE[hash % PALETTE.length];
    }

    function makeSvgElement(tag, attributes = {}) {
      const element = document.createElementNS(SVG_NS, tag);
      for (const [name, value] of Object.entries(attributes)) {
        element.setAttribute(name, value);
      }
      return element;
    }

    function addText(parent, text, x, y, className, maxLength = 26) {
      const element = makeSvgElement("text", {
        x,
        y,
        "text-anchor": "middle",
        class: className
      });
      const value = String(text || "");
      element.textContent = value.length > maxLength ? `${value.slice(0, maxLength - 1)}...` : value;
      parent.appendChild(element);
    }

    function showTooltip(node, event) {
      tooltip.replaceChildren();
      const fields = [
        ["Name", node.name],
        ["Size", node.size_human],
        ["Path", node.path],
        ["Type", node.type]
      ];
      if (node.error) {
        fields.push(["Error", node.error]);
      }
      for (const [label, value] of fields) {
        const line = document.createElement("div");
        const labelSpan = document.createElement("span");
        labelSpan.className = "tooltip-label";
        labelSpan.textContent = `${label}: `;
        const valueSpan = document.createElement("span");
        valueSpan.textContent = value || "";
        line.append(labelSpan, valueSpan);
        tooltip.appendChild(line);
      }
      tooltip.hidden = false;
      moveTooltip(event);
    }

    function moveTooltip(event) {
      if (!Number.isFinite(event.clientX) || !Number.isFinite(event.clientY)) {
        const targetRect = event.target && event.target.getBoundingClientRect
          ? event.target.getBoundingClientRect()
          : svg.getBoundingClientRect();
        tooltip.style.left = `${Math.max(8, targetRect.left + 12)}px`;
        tooltip.style.top = `${Math.max(8, targetRect.top + 12)}px`;
        return;
      }
      const offset = 14;
      const rect = tooltip.getBoundingClientRect();
      let left = event.clientX + offset;
      let top = event.clientY + offset;
      if (left + rect.width > window.innerWidth) {
        left = event.clientX - rect.width - offset;
      }
      if (top + rect.height > window.innerHeight) {
        top = event.clientY - rect.height - offset;
      }
      tooltip.style.left = `${Math.max(8, left)}px`;
      tooltip.style.top = `${Math.max(8, top)}px`;
    }

    function hideTooltip() {
      tooltip.hidden = true;
    }

    function drillInto(node) {
      resolveDeferred(node);
      if (!isFolder(node)) {
        return;
      }
      history.push(currentNode);
      currentNode = node;
      render();
    }

    function drawSegments(parent, startAngle, endAngle, depth) {
      if (depth > MAX_DEPTH) {
        return;
      }
      const innerRadius = CENTER_RADIUS + depth * RING_WIDTH;
      const outerRadius = innerRadius + RING_WIDTH - RING_GAP;
      weightedSegments(parent, startAngle, endAngle).forEach((segment, index) => {
        if (segment.end - segment.start <= 0.0001) {
          return;
        }
        const node = segment.node;
        const path = makeSvgElement("path", {
          d: arcPath(innerRadius, outerRadius, segment.start, segment.end),
          fill: colorFor(node, depth, index),
          class: `segment ${isFolder(node) ? "folder" : ""} ${node.deferred ? "deferred" : ""}`,
          role: isFolder(node) ? "button" : "img",
          tabindex: "0",
          "aria-label": nodeLabel(node)
        });
        path.addEventListener("pointerenter", (event) => showTooltip(node, event));
        path.addEventListener("pointermove", moveTooltip);
        path.addEventListener("pointerleave", hideTooltip);
        path.addEventListener("focus", (event) => showTooltip(node, event));
        path.addEventListener("blur", hideTooltip);
        if (isFolder(node)) {
          path.addEventListener("click", () => drillInto(node));
          path.addEventListener("keydown", (event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              drillInto(node);
            }
          });
        }
        svg.appendChild(path);
        drawSegments(node, segment.start, segment.end, depth + 1);
      });
    }

    function detailRow(label, value, className = "") {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value || "";
      if (className) {
        dd.className = className;
      }
      details.append(dt, dd);
    }

    function renderDetails() {
      details.replaceChildren();
      detailRow("Name", currentNode.name);
      detailRow("Size", currentNode.size_human);
      detailRow("Path", currentNode.path);
      detailRow("Type", currentNode.type);
      if (currentNode.error) {
        detailRow("Error", currentNode.error, "error");
      }

      childrenList.replaceChildren();
      if (currentNode.deferred) {
        const pending = document.createElement("li");
        pending.className = "muted";
        pending.textContent = "Deeper entries load on drill-in (progressive tiers).";
        childrenList.appendChild(pending);
      }
      for (const child of childrenOf(currentNode)) {
        const item = document.createElement("li");
        const name = document.createElement("span");
        name.className = "child-name";
        name.textContent = nodeLabel(child);
        const size = document.createElement("span");
        size.className = "child-size";
        size.textContent = child.size_human;
        item.append(name, size);
        if (isFolder(child)) {
          item.style.cursor = "pointer";
          item.title = "Click to drill into this folder";
          item.addEventListener("click", () => drillInto(child));
        }
        childrenList.appendChild(item);
      }
    }

    function renderCenter() {
      svg.appendChild(makeSvgElement("circle", {
        cx: CENTER,
        cy: CENTER,
        r: CENTER_RADIUS - 8,
        class: "center-disc"
      }));
      addText(svg, currentNode.name, CENTER, CENTER - 14, "center-title", 24);
      addText(svg, currentNode.size_human, CENTER, CENTER + 10, "center-meta", 28);
      addText(svg, currentNode.type, CENTER, CENTER + 32, "center-meta", 28);
    }

    function renderEmptyState() {
      const children = childrenOf(currentNode);
      const allZero = children.length > 0 && children.every((child) => nodeSize(child) === 0);
      if (!children.length) {
        emptyState.textContent = "No child entries to show for the selected node.";
      } else if (allZero) {
        emptyState.textContent = "All child entries are zero bytes, so sectors are shown with equal width.";
      } else {
        emptyState.textContent = "";
      }
      if (!children.length) {
        addText(svg, "No child entries", CENTER, CENTER + CENTER_RADIUS + 42, "empty-note", 40);
      }
    }

    function render() {
      svg.replaceChildren();
      svg.appendChild(makeSvgElement("title", { id: "chart-title" })).textContent = "Sunburst disk usage";
      svg.appendChild(makeSvgElement("desc", { id: "chart-desc" })).textContent =
        "Circular sunburst diagram with sector angles proportional to file and folder sizes.";
      drawSegments(currentNode, 0, TAU, 0);
      renderCenter();
      renderEmptyState();
      renderDetails();

      const trail = pathParts(data, currentNode) || [currentNode];
      breadcrumb.textContent = trail.map((node) => node.name).join(" / ");
      backButton.disabled = history.length === 0;
      rootButton.disabled = currentNode === data;
    }

    backButton.addEventListener("click", () => {
      const previous = history.pop();
      if (previous) {
        currentNode = previous;
        render();
      }
    });
    rootButton.addEventListener("click", () => {
      currentNode = data;
      history = [];
      render();
    });
    window.addEventListener("scroll", hideTooltip, { passive: true });

    updateLoadStatus();
    render();
    scheduleTierLoading();
  </script>
</body>
</html>
"""
    return (
        template.replace("__TITLE__", title)
        .replace("__META__", meta_payload)
        .replace("__TIER1__", tier1_payload)
        .replace("__TIER2__", tier2_payload)
        .replace("__TIER3__", tier3_payload)
    )


def dated_output_path(output_path: Path) -> Path:
    """Insert local date and time before the extension for versioned reports."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    suffix = output_path.suffix or ".html"
    return output_path.with_name(f"{output_path.stem}_{stamp}{suffix}")


def write_report(data: Node, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    html_text = report_html(data)
    built = time.perf_counter()
    output_path.write_text(html_text, encoding="utf-8")
    written = time.perf_counter()
    print(
        f"Report build: {built - started:.1f}s, "
        f"write: {written - built:.1f}s, "
        f"size: {human_size(output_path.stat().st_size)}",
        file=sys.stderr,
    )


def collect_names(node: Node) -> set[str]:
    names = {str(node["name"])}
    for child in node.get("children", []):
        names.update(collect_names(child))
    return names


def create_smoke_tree(root: Path) -> set[str]:
    smoke_root = root / "smoke_root"
    nested = smoke_root / "nested_dir" / "deeper"
    empty = smoke_root / "empty_dir"
    nested.mkdir(parents=True)
    empty.mkdir()
    (smoke_root / "alpha.txt").write_text("alpha\n", encoding="utf-8")
    (smoke_root / "notes with spaces.txt").write_text("notes\n", encoding="utf-8")
    (nested / "beta.bin").write_bytes(b"b" * 1536)

    expected = {
        "smoke_root",
        "alpha.txt",
        "notes with spaces.txt",
        "empty_dir",
        "nested_dir",
        "deeper",
        "beta.bin",
    }

    try:
        os.symlink(smoke_root / "alpha.txt", smoke_root / "alpha_link.txt")
    except OSError:
        pass
    else:
        expected.add("alpha_link.txt")

    return expected


def run_smoke() -> Path:
    with tempfile.TemporaryDirectory(prefix="sunburst_disk_scanner_") as tmp:
        tmp_path = Path(tmp)
        expected = create_smoke_tree(tmp_path)
        data = scan_path(tmp_path / "smoke_root")
        write_report(data, SMOKE_REPORT)
        html_text = SMOKE_REPORT.read_text(encoding="utf-8")

    data_names = collect_names(data)
    missing_from_data = sorted(expected - data_names)
    missing_from_html = sorted(name for name in expected if name not in html_text)
    ui_markers = (
        "id=\"sunburst\"",
        "id=\"scan-tier-1\"",
        "id=\"scan-tier-2\"",
        "id=\"scan-tier-3\"",
        "Back",
        "Root",
        "Sunburst disk usage",
        "function arcPath",
        "function showTooltip",
        "addEventListener(\"click\"",
        "Path",
        "KB",
        "scheduleTierLoading",
    )
    missing_ui = [marker for marker in ui_markers if marker not in html_text]

    if missing_from_data or missing_from_html or missing_ui:
        details = []
        if missing_from_data:
            details.append(f"data missing: {', '.join(missing_from_data)}")
        if missing_from_html:
            details.append(f"html missing: {', '.join(missing_from_html)}")
        if missing_ui:
            details.append(f"ui markers missing: {', '.join(missing_ui)}")
        raise RuntimeError("; ".join(details))

    return SMOKE_REPORT


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan a directory tree and generate a self-contained sunburst HTML report.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Path to scan. Defaults to the current directory.",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=str(DEFAULT_REPORT),
        help=f"Output HTML file. Defaults to {DEFAULT_REPORT}.",
    )
    parser.add_argument(
        "--dated",
        action="store_true",
        help=(
            "Append local timestamp _YYYY-MM-DD_HHMMSS before .html so each run keeps "
            "a separate file for history."
        ),
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=f"Run a self-check and write {SMOKE_REPORT.name} in the project directory.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.smoke:
        try:
            report_path = run_smoke()
        except Exception as exc:  # noqa: BLE001 - CLI boundary prints concise failure.
            print(f"Smoke failed: {error_text(exc)}", file=sys.stderr)
            return 1
        print(f"Smoke passed: {report_path}")
        return 0

    target = Path(args.path)
    output_path = Path(args.output)
    if args.dated:
        output_path = dated_output_path(output_path)

    scan_started = time.perf_counter()
    data = scan_path(target)
    scan_finished = time.perf_counter()
    print(
        f"Scan finished in {scan_finished - scan_started:.1f}s "
        f"({data['size_human']})",
        file=sys.stderr,
    )

    try:
        write_report(data, output_path)
    except OSError as exc:
        print(f"Could not write report: {error_text(exc)}", file=sys.stderr)
        return 1

    print(f"Scanned: {safe_path_string(target)}")
    print(f"Report: {safe_path_string(output_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
