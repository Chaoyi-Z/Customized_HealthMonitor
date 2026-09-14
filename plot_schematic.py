import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import matplotlib.patheffects as pe

fig, ax = plt.subplots(figsize=(14, 9))
ax.set_xlim(0, 14)
ax.set_ylim(0, 9)
ax.axis("off")
fig.patch.set_facecolor("#1a1a2e")
ax.set_facecolor("#1a1a2e")

COLORS = {
    "usbc":    "#e94560",
    "esp32":   "#0f3460",
    "bq":      "#16213e",
    "max":     "#533483",
    "pad":     "#2d4a3e",
    "border_usbc":  "#ff6b6b",
    "border_esp32": "#4d9de0",
    "border_bq":    "#e15554",
    "border_max":   "#a855f7",
    "border_pad":   "#4ecca3",
    "text":    "#e0e0e0",
    "label":   "#a0a0a0",
    "power":   "#ffd700",
    "i2c":     "#4ecca3",
    "usb":     "#ff9f43",
    "int":     "#fd79a8",
    "gnd":     "#636e72",
}

def block(ax, x, y, w, h, label, sublabels, facecolor, edgecolor, fontsize=9):
    box = FancyBboxPatch((x, y), w, h,
                         boxstyle="round,pad=0.08",
                         facecolor=facecolor, edgecolor=edgecolor,
                         linewidth=2, zorder=3)
    ax.add_patch(box)
    ax.text(x + w/2, y + h - 0.28, label,
            ha="center", va="top", fontsize=fontsize+1, fontweight="bold",
            color=COLORS["text"], zorder=4)
    for i, sl in enumerate(sublabels):
        ax.text(x + w/2, y + h - 0.62 - i*0.32, sl,
                ha="center", va="top", fontsize=fontsize-1,
                color=COLORS["label"], zorder=4)

def arrow(ax, x1, y1, x2, y2, color, label="", lw=1.8, style="-|>"):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle=style, color=color, lw=lw),
                zorder=2)
    if label:
        mx, my = (x1+x2)/2, (y1+y2)/2
        ax.text(mx + 0.08, my, label, fontsize=7.5, color=color,
                va="center", zorder=5,
                bbox=dict(boxstyle="round,pad=0.15", facecolor="#1a1a2e",
                          edgecolor="none", alpha=0.85))

def line(ax, xs, ys, color, lw=1.8, ls="-"):
    ax.plot(xs, ys, color=color, lw=lw, ls=ls, zorder=2)

# ── Blocks ─────────────────────────────────────────────────────────────────
# USB-C  (top-left)
block(ax, 0.4, 6.6, 2.2, 2.0, "USB-C",
      ["VBUS (5V)", "D+ / D−", "GND"],
      COLORS["usbc"], COLORS["border_usbc"])

# ESP32-S3  (top-right)
block(ax, 9.8, 5.8, 3.8, 2.8, "ESP32-S3-MINI-1",
      ["3V3  ← BQ.SYS", "IO19/IO20  (USB D+/D−)",
       "IO8/IO9  (I²C SDA/SCL)",
       "IO4  (MAX INT)", "IO6  (BQ INT)",
       "IO0  (boot pad)", "EN   (chip enable)"],
      COLORS["esp32"], COLORS["border_esp32"])

# BQ25120A  (center)
block(ax, 4.6, 3.5, 4.4, 4.8, "BQ25120A PMIC",
      ["WCSP-25  (2.5 × 2.6 mm)",
       "VIN  ← USB-C / charge pads",
       "BAT  ↔ LiPo 100 mAh",
       "SYS  → 3.3V (ESP32 + MAX VDD)",
       "LDO  → 1.8V (MAX VLED+)",
       "MR   ← SW_PWR button",
       "SDA/SCL/INT → I²C bus"],
      COLORS["bq"], COLORS["border_bq"])

# MAX30102  (bottom-right)
block(ax, 9.8, 1.4, 3.8, 2.8, "MAX30102",
      ["Flex tail / OESIP-14",
       "VDD   ← 3.3V (BQ.SYS)",
       "VLED+ ← 1.8V (BQ.LDO)",
       "SDA/SCL ← I²C bus",
       "INT  → ESP32 IO4"],
      COLORS["max"], COLORS["border_max"])

# Emergency boot pads  (bottom-left)
block(ax, 0.4, 1.2, 2.8, 2.2, "Emergency Boot Pads",
      ["TP_BOOT (GPIO0 → GND)",
       "TP_EN   (reset)",
       "TP_GND"],
      COLORS["pad"], COLORS["border_pad"], fontsize=8)

# LiPo battery  (bottom-center)
block(ax, 4.6, 0.5, 2.4, 1.6, "LiPo 100 mAh",
      ["3.5 × 10 × 15 mm", "~2.2 g"],
      "#2a2a1e", "#ffd700")

# SW_PWR button  (left-center)
block(ax, 0.4, 4.0, 2.2, 1.2, "SW_PWR",
      ["Tactile 3.5×3.5 mm"],
      "#1a2a1a", COLORS["border_pad"], fontsize=8)

# ── Power connections (gold) ────────────────────────────────────────────────
# USB-C VBUS → BQ VIN
arrow(ax, 2.6, 7.6,  4.6, 6.8, COLORS["power"], "5V VBUS")
# BQ SYS → ESP32 3V3
arrow(ax, 9.0, 6.8,  9.8, 7.0, COLORS["power"], "3.3V SYS")
# BQ SYS → MAX VDD
arrow(ax, 9.0, 5.2,  9.8, 3.0, COLORS["power"], "3.3V VDD")
# BQ LDO → MAX VLED+
arrow(ax, 9.0, 4.6,  9.8, 2.6, "#c0a020", "1.8V VLED+")
# BQ BAT ↔ LiPo
arrow(ax, 6.8, 3.5,  6.8, 2.1, COLORS["power"], "BAT")

# ── I²C bus (teal) ─────────────────────────────────────────────────────────
# ESP32 IO8/9 → I²C bus node
line(ax, [9.8, 9.0, 9.0], [6.65, 6.65, 3.8], COLORS["i2c"], lw=2)
# I²C bus → BQ SDA/SCL
arrow(ax, 9.0, 3.8,  9.0, 3.8, COLORS["i2c"])   # endpoint marker
# label
ax.text(9.05, 5.2, "I²C bus\n(SDA/SCL)", fontsize=7.5, color=COLORS["i2c"],
        va="center", zorder=5)
# I²C bus → MAX SDA/SCL
line(ax, [9.0, 9.8], [3.8, 3.8], COLORS["i2c"], lw=2)

# ── Interrupt lines (pink) ─────────────────────────────────────────────────
# MAX INT → ESP32 IO4
arrow(ax, 9.8, 2.2,  9.8, 5.8, COLORS["int"], "INT→IO4", lw=1.5, style="->")
# BQ INT → ESP32 IO6
arrow(ax, 9.0, 5.5,  9.8, 6.1, COLORS["int"], "INT→IO6", lw=1.5, style="->")

# ── USB D+/D− (orange) ─────────────────────────────────────────────────────
arrow(ax, 2.6, 7.0,  9.8, 6.45, COLORS["usb"], "D+/D−", lw=1.5, style="->")

# ── SW_PWR → BQ MR ────────────────────────────────────────────────────────
arrow(ax, 2.6, 4.6,  4.6, 5.1, COLORS["border_pad"], "MR/PB", lw=1.5)

# ── Emergency pads → ESP32 ────────────────────────────────────────────────
line(ax, [3.2, 3.2, 9.8], [1.8, 8.4, 8.4], COLORS["border_pad"], lw=1.2, ls="--")
ax.text(6.5, 8.55, "GPIO0 (boot pad) — bridge to GND only if OTA fails",
        fontsize=7, color=COLORS["border_pad"], ha="center", zorder=5)

# ── Legend ─────────────────────────────────────────────────────────────────
legend_items = [
    mpatches.Patch(color=COLORS["power"], label="Power rails"),
    mpatches.Patch(color=COLORS["i2c"],   label="I²C bus"),
    mpatches.Patch(color=COLORS["usb"],   label="USB D+/D−"),
    mpatches.Patch(color=COLORS["int"],   label="Interrupt lines"),
    mpatches.Patch(color=COLORS["border_pad"], label="Button / boot pads"),
]
ax.legend(handles=legend_items, loc="lower left", fontsize=8,
          facecolor="#0d0d1a", edgecolor="#444", labelcolor=COLORS["text"],
          framealpha=0.9)

ax.set_title("ESP32 Health Monitor — Schematic Block Diagram (v2)",
             fontsize=13, color=COLORS["text"], pad=12, fontweight="bold")

plt.tight_layout()
plt.savefig("schematic_block_diagram.png", dpi=150, bbox_inches="tight",
            facecolor=fig.get_facecolor())
plt.show()
print("Saved: schematic_block_diagram.png")
