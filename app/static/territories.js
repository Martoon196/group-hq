/* The territory map: Leaflet markers at district centroids, coloured by state, with a popup of headline figures.
   Data comes from the <script type="application/json" id="territory-data"> block the page embeds; nothing is
   fetched. Every figure in a popup is a proposal while the model's assumptions are unconfirmed, so the popup
   wears the same [CONFIRM] badge as the screens. Leaflet is loaded from unpkg (the same tags Property Predator serves live) with an integrity hash; if the
   browser refused it (or unpkg is unreachable) the page says so instead of failing silently. */
(function () {
  "use strict";
  var holder = document.getElementById("territory-data");
  var note = document.getElementById("map-note");
  if (!holder) return;
  var data;
  try { data = JSON.parse(holder.textContent); } catch (e) { if (note) note.textContent = "The map data could not be read."; return; }
  if (!window.L) {
    if (note) note.textContent = "The map library did not load: the Leaflet script from unpkg.com was blocked or its integrity hash did not match. The list view has the same figures.";
    return;
  }
  var COLOURS = { open: "#8A8A8A", reserved: "#D98E04", core: "#1F9D55", hq: "#E0287A" };
  var LABELS = { open: "open", reserved: "reserved", core: "core", hq: "head office" };

  function esc(v) {
    return String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function row(k, v, badge) {
    if (v === null || v === undefined || v === "") v = "-";
    return "<tr><td class=\"k\">" + esc(k) + "</td><td>" + esc(v) + (badge ? " <span class=\"confirm\">[CONFIRM]</span>" : "") + "</td></tr>";
  }
  function popup(d) {
    var held = LABELS[d.state] || d.state;
    if (d.partner_id) held += ", partner " + d.partner_id;
    if (d.reserved_until) held += ", until " + d.reserved_until;
    var html = "<h3>" + esc(d.district) + " <small>" + esc(d.la_name || "") + "</small></h3><table>" +
      row("State", held, false) +
      row("Band", d.band, d.confirm) +
      row("PRS households", d.prs_households === null ? null : Number(d.prs_households).toLocaleString("en-GB"), false) +
      row("Fee per unit", d.achievable_fee, d.confirm) +
      row("Units y1 / y3", d.units_y1 === null ? null : d.units_y1 + " / " + d.units_y3, d.confirm) +
      row("Partner, year one", d.partner_net_y1, d.confirm) +
      row("HQ, year one", d.hq_contribution_y1, d.confirm) +
      row("Upfront / monthly", (d.upfront_fee || "-") + " / " + (d.monthly_fee || "-"), d.confirm) +
      "</table>";
    if (data.sample) html += "<p><strong>SAMPLE DATA</strong>: not a fact about this district.</p>";
    html += "<p><a href=\"/territories/" + encodeURIComponent(d.district) + "\">Open " + esc(d.district) + "</a></p>";
    return html;
  }

  var map = L.map("territory-map", { scrollWheelZoom: true });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: "&copy; <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors"
  }).addTo(map);
  var bounds = [];
  var plotted = 0;
  (data.districts || []).forEach(function (d) {
    if (d.lat === null || d.lon === null || d.lat === undefined) return;
    var marker = L.circleMarker([d.lat, d.lon], {
      radius: 7, color: "#fff", weight: 1, fillColor: COLOURS[d.state] || COLOURS.open, fillOpacity: 0.9
    }).addTo(map);
    marker.bindPopup(popup(d));
    marker.bindTooltip(esc(d.district) + " (" + esc(LABELS[d.state] || d.state) + ")");
    bounds.push([d.lat, d.lon]);
    plotted += 1;
  });
  if (bounds.length) map.fitBounds(bounds, { padding: [24, 24], maxZoom: 12 });
  else map.setView([54.4, -3.0], 6);
  if (note) note.textContent = plotted + " marker" + (plotted === 1 ? "" : "s") + " plotted" + (data.sample ? " from SAMPLE data" : "") + ".";
})();
