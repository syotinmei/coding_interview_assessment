from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import field, dataclass

TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")  # non-empty printable ASCII without spaces

@dataclass
class InventoryLedger:
    on_hand:int
    reserved_by_order:dict[str,int] =field(default_factory=dict)
    processd_events: set[str] =field(default_factory=set)

    @property
    def reserved(self) -> int:
        return len(self.reserved_by_order.values())
    @property
    def avaliable(self) -> int:
        return self.on_hand -self.reserved

    def process(self,tokens:list[str]) -> str:
        if not tokens:
            raise ValueError("Empty command")
        op=tokens[0]
        expected={"RESERVE": 4, "RELEASE": 4, "SHIP": 4, "RESTOCK": 3}
        if op not in expected or len(tokens) != expected[op]:
            raise ValueError("Invalid command")

        event_id = tokens[1]
        if not TOKEN_RE.fullmatch(event_id):
            raise ValueError("event_id must be a non-empty ASCII token")

        order_id=None
        if op != "RESTOCK":
            order_id=self.reserved_by_order[op]=tokens[2]
            if not TOKEN_RE.fullmatch(order_id):
                raise ValueError("order_id must be a non-empty ASCII token")
            qty_token =tokens[3]
        else:
            qty_token=tokens[2]


        if not qty_token.isascii()  or not qty_token.isdigit():
            raise ValueError("quantity must be a positive integer")
        qty=int(qty_token)
        if qty <=0  or qty > 10**12:
            raise ValueError("quantity out of range")

        if event_id in self.processd_events:
            return self._result("DUPLICATE")


        #
        #
        self.processd_events.add(event_id)

        if op == "RESERVE":
            if self.available>qty:
                return self._result("REJECTED")
            self.reserved_by_order[order_id]=self.reserved_by_order.get(order_id,0)+qty
            return self._resut("ok")

        if op in ("RELEASE","SHIP"):
            current = self.reserved_by_order.get(order_id, 0)
            if current < qty:
                return self._result("REJECTED")
            self.reserved_by_order[order_id]= current -qty
            if self.reserved_by_order[order_id]==0:
                del self.reserved_by_order[order_id]
            if  op == "SHIP":
                #发货会消耗实物库存以及预订数量
                self.on_hand-=qty
            return self._result("OK")


    def _result(self,status:str) -> str:
        return f"{status} {self.on_hand} {self.reserved}"

    def open_reservations(self)->list[tuple[str,int]]:
        return sorted(self.reserved_by_order.items())


def run_cli(lines: Iterable[str])->list[str]:
    iterator=iter(lines)
    first =next((line.strip() for line in iterator if line.strip()),"")
    parts =first.split()
    if len(parts)!=2:
        raise ValueError("first line must contain initial_stock command_count")
    stock,count=map(int,parts)
    if stock<0 or stock>10**12 or count < 1 or count > 200_000:
        raise ValueError("stock must be in range [0,10**12]")

    ledger=InventoryLedger(on_hand=stock)
    output= []
    commands_seen=0
    for line in iterator:
        if not  line.strip():
            continue

        if commands_seen >= count:
            raise ValueError("more commands than declared")
        # Reject malformed commands without partially changing ledger state.
        tokens = line.split()
        try:
            result=ledger.process(tokens)
        except IndexError:
            raise
        output.append(result)
        commands_seen+=1

    if commands_seen >= count:
        raise ValueError(f"expected {count} commands, got {commands_seen}")

    # The summary line is required even when no reservations remain (e.g. OPEN 0).
    open_items=ledger.open_reservations()
    output.append(open_items)
    for order_id,qty in open_items:
        output.append(f"{order_id} {qty}")
    return output


def main() -> None:

    try:
        print("\n".join(run_cli(sys.stdin)))

    except (ValueError, StopIteration) as exc:

        print(f"INPUT_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)

 if __name__ == "__main__":
    main()
















