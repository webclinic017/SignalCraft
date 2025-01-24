import duckdb
import logging

from app.models.signal import Signal
from app.strategies.base import get_ticker_data, get_ticker_data_by_timeframe
from app.strategies.market_profile_strategy import MarketProfileStrategy
from app.strategies.markov_prediction_strategy import MarkovPredictionStrategy
from app.strategies.base import BaseStrategy
from alpaca.data import TimeFrame

logger = logging.getLogger("app")


class StrategyHandler():
    def __init__(self, tickers, db_base_path="dbs", timeframe=TimeFrame.Minute):
        super().__init__()
        self.db_base_path = db_base_path
        self.tickers = tickers
        self.timeframe = timeframe
        self.markov_prediction = MarkovPredictionStrategy(db_base_path=self.db_base_path)
        self.market_profile_strategy = MarketProfileStrategy(timeframe=self.timeframe)
        self.strategies = {
            'markov': self.markov_prediction,
            'market_profile': self.market_profile_strategy
        }

    def generate_signals(self, is_backtest=False, backtest_data=None):
        signal_data = dict()

        for ticker in self.tickers:
            connection = duckdb.connect(f"{self.db_base_path}/{ticker}_{self.timeframe}_data.db")
            if is_backtest:
                ticker_data = get_ticker_data_by_timeframe(ticker, connection, timeframe=self.timeframe, db_base_path=self.db_base_path, end=backtest_data['end'])
            else:
                ticker_data = get_ticker_data(ticker, connection, timeframe=self.timeframe, db_base_path=self.db_base_path)    
            connection.close()
            most_recent_ticker_datetime = ticker_data['timestamp'].max()
            for strategy in self.strategies.values():
                signal: Signal = strategy.generate_signal(ticker, ticker_data)
                if signal is not None and signal.action is not None:
                    logging.info(f"Signal generated for {ticker}: {signal.action}")
                    signal.timestamp = most_recent_ticker_datetime
                    signal_data[ticker] = signal_data
            
        return signal_data
