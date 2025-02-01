import logging
import duckdb
from datetime import datetime, timedelta
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.enums import DataFeed
from alpaca.data import StockBarsRequest
from alpaca.data import TimeFrame

logger = logging.getLogger("app")


class DataHandler():
    def __init__(self, tickers, api_key, api_secret, db_base_path, timeframe=TimeFrame.Minute, is_backtest=False):
        super().__init__()
        self.tickers = tickers  # List of tickers to subscribe to
        self.db_base_path = db_base_path  # Base path for database files
        self.data_store = StockHistoricalDataClient(api_key, api_secret)   
        self.timeframe = timeframe
        self.is_backtest = is_backtest
        self.api_key = api_key
        self.api_secret = api_secret
        self.is_stream_subscribed = False

    def fetch_data(self, start=None, end=None, days=1, use_most_recent=False):
        """
        Fetch candle data for the specified tickers and timeframe.
        start is the start date for the data fetch. If None, it defaults to the current date minus `days`.
        end is the end date for the data fetch. If None, it defaults to the current date.
        days is the number of days to fetch data for. If None, it defaults to 1. start and end if specified will override this.
        use_most_recent is a flag to set start as the most recent candle data timestamp.
        Iterates by the number of days specified in the `days` parameter.
        """
        end = datetime.now() if end is None else end
        start = end - timedelta(days=days) if start is None else start
        
        # set start value to the most recent candle timestamp
        if use_most_recent:
            # find the most recent candle timestart as `start``
            oldest_candle = None
            for ticker in self.tickers:
                
                connection = duckdb.connect(f"{self.db_base_path}/{ticker}_{self.timeframe}_data.db")
                # get most recent candle from the db
                most_recent_candle_data = connection.sql(
                    f"SELECT * FROM ticker_data ORDER BY timestamp DESC LIMIT 1"
                ).df()
                connection.close()
                last_candle = most_recent_candle_data["timestamp"].iloc[0] if not most_recent_candle_data.empty else None
                if oldest_candle and last_candle and last_candle < oldest_candle:
                    oldest_candle = last_candle
                elif not oldest_candle:
                    oldest_candle = last_candle
            start = oldest_candle if oldest_candle else start
        

        try:
            curr_start = start
            curr_end = start + timedelta(days=1)
            while curr_start <= end:
                logger.info(f"Fetching data for tickers from {curr_start} to {curr_end}")
                data = None
                request = StockBarsRequest(
                    symbol_or_symbols=self.tickers,
                    start=curr_start,
                    end=curr_end,
                    timeframe=self.timeframe,
                )
                try:
                    data = self.data_store.get_stock_bars(request)
                except Exception as e:
                    logger.error(f"Error fetching market data for ticker:{ticker} error; {e}")
                
                if data is None or data.data is None:
                    logger.info("No data received", data)
                else:    
                    logger.info(f"Data received for {ticker} from {curr_start} to {curr_end}")
                    self.save_market_data(data.data)
                logger.info(f"Data saved for tickers")
                curr_start = curr_end
                curr_end = curr_start + timedelta(days=1)
        except Exception as e:
            logger.error(f"Error fetching market data: {e}")
            return None
        
    def fetch_most_recent_prices(self):
        """
        Fetch the most recent candle data for the specified tickers.
        """
        try:
            ticker_to_price_map = dict()
            for ticker in self.tickers:
                connection = duckdb.connect(f"{self.db_base_path}/{ticker}_{self.timeframe}_data.db")
                most_recent_candle_data = connection.sql(
                    f"SELECT * FROM ticker_data ORDER BY timestamp DESC LIMIT 1"
                ).df()
                connection.close()
                last_candle = most_recent_candle_data["timestamp"].iloc[0] if not most_recent_candle_data.empty else None
                if last_candle:
                    price = most_recent_candle_data["close"].iloc[0]
                    ticker_to_price_map[ticker] = price

        except Exception as e:
            logger.error(f"Error fetching most recent prices: {e}")
            return None
        
        return ticker_to_price_map
    
    def get_backtest_data(self):
        data = dict()
        for ticker in self.tickers:
            conn = duckdb.connect(f"{self.db_base_path}/{ticker}_{self.timeframe}_data.db")
            ticker_data = conn.sql(f"SELECT * FROM ticker_data ORDER BY timestamp ASC").df()
            conn.close()
            data[ticker] = ticker_data
        return data
    
    async def handle_stream_data(self, bar_data):
        for key, val in bar_data.items():
            if key not in self.tickers:
                continue
            else:
                logger.info(f"Received data for {key}: {val}")
                value_str = f"('{val.timestamp}', '{key}', {val.open}, {val.high}, {val.low}, {val.close}, {val.volume}, {val.vwap})"
                
                self.save_streaming_ticker_data_to_db(key, value_str)
                

    def save_market_data(self, data: dict):
        for ticker in data.keys():
            ticker_data = data.get(ticker, [])
            value_strs = []
            for row in ticker_data:
                value_str = f"('{row.timestamp}', '{ticker}', {row.open}, {row.high}, {row.low}, {row.close}, {row.volume}, {row.vwap})"
                logger.debug(f"candle values for {ticker}: {value_str}")
                value_strs.append(value_str)
            self.save_to_db(ticker, value_strs)
            
            logger.info('Data saved for ticker {}'.format(ticker))

    def save_to_db(self, ticker, value_strs):
        value_str = ", ".join(value_strs)
        db_path = f"{self.db_base_path}/{ticker}_{self.timeframe}_data.db"
        conn = duckdb.connect(db_path)
        conn.execute(f"INSERT OR IGNORE INTO ticker_data VALUES {value_str}")
        conn.close()

    def save_streaming_ticker_data_to_db(self, ticker, value_str):
        conn = duckdb.connect(f"{self.db_base_path}/{ticker}_{self.timeframe}_data.db")
        conn.execute(f"INSERT OR IGNORE INTO ticker_data VALUES {value_str}")
        conn.close()

    async def subscribe_to_data_stream(self):
        stream = StockDataStream(api_key=self.api_key, secret_key=self.api_secret, feed=DataFeed.IEX)

        stream.subscribe_quotes(self.handle_stream_data, *self.tickers)
        stream.run()
        self.is_stream_subscribed = True
        logger.info('Subscribed to data stream')