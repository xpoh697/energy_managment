import logging
from .strategy_base import StrategyEngine as StrategyEngineBase
from .strategy_sell import StrategySell
from .strategy_buy import StrategyBuy

_LOGGER = logging.getLogger(__name__)

class StrategyEngine(StrategyEngineBase):
    """
    Dispatcher engine that delegates strategy calculations to specialized modules.
    Keeps all simulation and helper methods from StrategyEngineBase.
    """
    
    def __init__(self, manager):
        super().__init__(manager)
        # Instantiate specialized engines once to preserve their internal caches
        self._buy_engine = StrategyBuy(manager)
        self._sell_engine = StrategySell(manager)

    def clear_cache(self):
        """Clears cache for all specialized engines."""
        super().clear_cache()
        self._buy_engine.clear_cache()
        self._sell_engine.clear_cache()
    
    def get_market_strategy(self, mode="buy", allow_recalc=True):
        """Delegates calculation to the appropriate specialized strategy class."""
        if mode == "sell":
            return self._sell_engine.get_market_strategy(mode, allow_recalc=allow_recalc)
        else:
            # v11.9.714: Always fetch sell strategy first to account for planned sales in Buy simulation
            # (Buy is now aware of Sell's intentions)
            sell_res = self._sell_engine.get_market_strategy("sell", allow_recalc=allow_recalc)
            return self._buy_engine.get_market_strategy(mode, sell_commands=sell_res.get("raw_commands", {}), allow_recalc=allow_recalc)
