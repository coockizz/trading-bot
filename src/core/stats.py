"""Suivi de performance : P&L, win rate, drawdown, exposition.

Le P&L realise est calcule en cout moyen pondere : chaque fill qui reduit
l'inventaire cristallise un gain ou une perte contre le prix moyen d'entree.
C'est la convention de Binance Futures, ce qui evite un ecart entre les stats
du bot et celles de l'exchange.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.core.logger import get_logger
from src.models.domain import Fill, Side
from src.utils.helpers import utc_day_start

logger = get_logger("stats")


@dataclass(slots=True)
class ClosedTrade:
    """Portion d'inventaire fermee par un fill, avec son P&L net de frais."""

    timestamp: float
    side: Side
    quantity: float
    entry_price: float
    exit_price: float
    gross_pnl: float
    fees: float

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees


@dataclass(slots=True)
class StatsSnapshot:
    """Vue serialisable des stats, destinee au fichier JSON et au dashboard."""

    generated_at: str
    total_pnl: float
    realized_pnl: float
    unrealized_pnl: float
    daily_pnl: float
    total_fees: float
    num_fills: int
    num_closed_trades: int
    win_rate_pct: float
    max_drawdown_pct: float
    current_drawdown_pct: float
    average_exposure_usdt: float
    inventory_qty: float
    average_entry_price: float


class StatsTracker:
    """Agrege les fills et les variations d'equity en metriques de performance."""

    def __init__(self, initial_equity: float, *, trades_csv: str | Path | None = None) -> None:
        if initial_equity <= 0:
            raise ValueError(f"initial_equity doit etre > 0, recu {initial_equity}")
        self._initial_equity = initial_equity
        self._peak_equity = initial_equity
        self._equity = initial_equity
        self._trades_csv = Path(trades_csv) if trades_csv else None

        self.gross_realized_pnl: float = 0.0
        self.unrealized_pnl: float = 0.0
        self.total_fees: float = 0.0
        self.inventory_qty: float = 0.0
        self.average_entry_price: float = 0.0
        self.num_fills: int = 0
        self.max_drawdown_pct: float = 0.0

        self.closed_trades: list[ClosedTrade] = []
        self._exposure_samples: list[float] = []

        self._day_start = utc_day_start()
        self._realized_at_day_start = 0.0
        self._unrealized_at_day_start = 0.0

    # ------------------------------------------------------------------ fills

    def record_fill(self, fill: Fill) -> float:
        """Integre un fill. Retourne le P&L realise net genere par ce fill."""
        if fill.quantity <= 0:
            raise ValueError(f"quantite de fill invalide : {fill.quantity}")

        self.num_fills += 1
        self.total_fees += fill.fee
        signed_qty = fill.quantity * fill.side.sign
        realized = 0.0

        opening = self.inventory_qty == 0 or (self.inventory_qty > 0) == (signed_qty > 0)
        if opening:
            self._extend_inventory(signed_qty, fill.price)
        else:
            realized = self._reduce_inventory(fill, signed_qty)

        self._append_trade_row(fill, realized)
        return realized

    def _extend_inventory(self, signed_qty: float, price: float) -> None:
        new_qty = self.inventory_qty + signed_qty
        total_cost = self.average_entry_price * abs(self.inventory_qty) + price * abs(signed_qty)
        self.inventory_qty = new_qty
        self.average_entry_price = total_cost / abs(new_qty) if new_qty else 0.0

    def _reduce_inventory(self, fill: Fill, signed_qty: float) -> float:
        """Ferme tout ou partie de l'inventaire, en gerant le retournement."""
        closable = min(abs(signed_qty), abs(self.inventory_qty))
        direction = 1 if self.inventory_qty > 0 else -1
        gross = (fill.price - self.average_entry_price) * closable * direction
        self.gross_realized_pnl += gross
        net = gross - fill.fee

        self.closed_trades.append(
            ClosedTrade(
                timestamp=fill.timestamp,
                side=fill.side,
                quantity=closable,
                entry_price=self.average_entry_price,
                exit_price=fill.price,
                gross_pnl=gross,
                fees=fill.fee,
            )
        )

        remainder = abs(signed_qty) - closable
        self.inventory_qty += signed_qty
        if abs(self.inventory_qty) < 1e-12:
            self.inventory_qty = 0.0
            self.average_entry_price = 0.0
        elif remainder > 0:
            # Retournement : le reliquat ouvre une position dans l'autre sens.
            self.average_entry_price = fill.price
        return net

    # --------------------------------------------------------------- equity

    def update_market(self, mark_price: float) -> None:
        """Reevalue l'inventaire au prix courant et met a jour le drawdown."""
        if mark_price <= 0:
            raise ValueError(f"mark_price doit etre > 0, recu {mark_price}")

        self.unrealized_pnl = (
            (mark_price - self.average_entry_price) * self.inventory_qty
            if self.inventory_qty
            else 0.0
        )
        self._exposure_samples.append(abs(self.inventory_qty) * mark_price)
        self._equity = self._initial_equity + self.total_pnl
        self._peak_equity = max(self._peak_equity, self._equity)
        self.max_drawdown_pct = max(self.max_drawdown_pct, self.current_drawdown_pct)
        self._roll_day_if_needed()

    def _roll_day_if_needed(self) -> None:
        today = utc_day_start()
        if today > self._day_start:
            logger.info(
                "Nouvelle journee UTC : P&L journalier remis a zero (jour precedent %s)",
                f"{self.daily_pnl:+.2f} USDT",
            )
            self._day_start = today
            self._realized_at_day_start = self.realized_pnl
            self._unrealized_at_day_start = self.unrealized_pnl

    # -------------------------------------------------------------- lectures

    @property
    def realized_pnl(self) -> float:
        """P&L cristallise, net de TOUS les frais payes (ouvertures comprises)."""
        return self.gross_realized_pnl - self.total_fees

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def daily_pnl(self) -> float:
        realized_today = self.realized_pnl - self._realized_at_day_start
        unrealized_delta = self.unrealized_pnl - self._unrealized_at_day_start
        return realized_today + unrealized_delta

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def current_drawdown_pct(self) -> float:
        if self._peak_equity <= 0:
            return 0.0
        return max(0.0, (self._peak_equity - self._equity) / self._peak_equity * 100.0)

    @property
    def win_rate_pct(self) -> float:
        if not self.closed_trades:
            return 0.0
        wins = sum(1 for t in self.closed_trades if t.net_pnl > 0)
        return wins / len(self.closed_trades) * 100.0

    @property
    def average_exposure_usdt(self) -> float:
        if not self._exposure_samples:
            return 0.0
        return sum(self._exposure_samples) / len(self._exposure_samples)

    def snapshot(self) -> StatsSnapshot:
        return StatsSnapshot(
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            total_pnl=round(self.total_pnl, 6),
            realized_pnl=round(self.realized_pnl, 6),
            unrealized_pnl=round(self.unrealized_pnl, 6),
            daily_pnl=round(self.daily_pnl, 6),
            total_fees=round(self.total_fees, 6),
            num_fills=self.num_fills,
            num_closed_trades=len(self.closed_trades),
            win_rate_pct=round(self.win_rate_pct, 2),
            max_drawdown_pct=round(self.max_drawdown_pct, 4),
            current_drawdown_pct=round(self.current_drawdown_pct, 4),
            average_exposure_usdt=round(self.average_exposure_usdt, 2),
            inventory_qty=round(self.inventory_qty, 8),
            average_entry_price=round(self.average_entry_price, 8),
        )

    # -------------------------------------------------------------- exports

    def write_json(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(asdict(self.snapshot()), indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _append_trade_row(self, fill: Fill, realized: float) -> None:
        """Ajoute une ligne au journal CSV des executions (format dashboard)."""
        if self._trades_csv is None:
            return
        self._trades_csv.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self._trades_csv.exists()
        with self._trades_csv.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if is_new:
                writer.writerow(
                    ["timestamp", "side", "price", "quantity", "fee", "realized_pnl", "level"]
                )
            writer.writerow(
                [
                    datetime.fromtimestamp(fill.timestamp, UTC).isoformat(timespec="seconds"),
                    fill.side.value,
                    f"{fill.price:.8f}",
                    f"{fill.quantity:.8f}",
                    f"{fill.fee:.8f}",
                    f"{realized:.8f}",
                    fill.level_index,
                ]
            )

    def log_summary(self) -> None:
        snap = self.snapshot()
        logger.info(
            "STATS | P&L total %+.2f USDT (realise %+.2f / latent %+.2f) | jour %+.2f | "
            "fills %d | trades %d | win rate %.1f%% | DD max %.2f%% | frais %.2f",
            snap.total_pnl,
            snap.realized_pnl,
            snap.unrealized_pnl,
            snap.daily_pnl,
            snap.num_fills,
            snap.num_closed_trades,
            snap.win_rate_pct,
            snap.max_drawdown_pct,
            snap.total_fees,
        )
