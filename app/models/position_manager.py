from alpaca.trading import TradingClient
from alpaca.trading.enums import OrderSide
from datetime import datetime
import logging
from app.models.position import Position
from app.models.signal import Signal

logger = logging.getLogger("app")


class PositionManager:
    def __init__(self, trading_client: TradingClient, backtest=False):
        self.trading_client = trading_client
        self.positions = {}  # ticker -> Position object
        self.pending_closes = set()  # tickers with pending close orders
        self.pending_orders = []  # List of pending new position orders
        self.is_backtest = backtest
        
        # Position sizing parameters
        self.max_position_size = 0.08  # 8% max per position
        self.position_step_size = 0.02  # 2% per trade for gradual building
        self.max_total_exposure = 1.6  # 160% total exposure (80% long + 80% short)
        
        # Backtest account data
        self.starting_balance = 30000  # Starting balance for backtest
        self.cash_balance = self.starting_balance
        self.equity = self.starting_balance
        self.unrealized_pnl = 0

        # Initialize current positions and pending orders
        self.update_positions()
        self.update_pending_orders()
    
    def calculate_target_position(self, ticker, price, side, target_pct=None):
        """
        Calculate target position size considering existing positions
        Args:
            ticker: Stock ticker
            price: Current price
            side: OrderSide.BUY or OrderSide.SELL
            target_pct: Target position size as % of equity (e.g. 0.08 for 8%)
        Returns target shares and whether to allow the trade
        """
        price = float(price)
        account = self.get_account_info()
        equity = account['equity']
        logger.debug('calculating target position for %r with equity %r at price %r and side %r', ticker, equity, price, side)
        
        # Calculate current total exposure excluding pending closes
        active_positions = {s: p for s, p in self.positions.items() 
                          if s not in self.pending_closes}
        total_exposure = sum(p.get_exposure(equity) for p in active_positions.values() if p is not None)
        
        if side == OrderSide.BUY:
            # Check if we're already at max exposure
            if total_exposure >= self.max_total_exposure:
                logger.debug("Maximum total exposure reached: %d", total_exposure)
                return 0, False
        
        # Use provided target_pct or default max_position_size
        position_size = target_pct if target_pct is not None else self.max_position_size
        target_position_value = equity * position_size
        current_position = active_positions.get(ticker)
        
        try:
            if current_position:
                # Position exists - check if we should add more
                current_exposure = current_position.get_exposure(equity)
                
                # Don't add if already at target size
                if side == OrderSide.BUY and current_exposure >= position_size:
                    logger.debug("Target position size reached for %r (%.1f%% exposure)", ticker, current_exposure)
                    return 0, False
                elif side == OrderSide.SELL and current_position.qty == 0:
                    logger.debug("No shares to sell for %r", ticker)
                    return 0, False
                
                # Don't add if position moving against us
                if current_position.pl_pct < -0.02:  # -2% loss threshold
                    logger.info("Position moving against us: %.1f%% P&L", current_position.pl_pct)
                    logger.info("MAYBE WE SHOULD SELL!!!")
                    return 0, False
                
                # Calculate remaining size to reach target
                remaining_size = target_position_value - (current_position.qty * price)
                return int(remaining_size / price), True
                
            else:
                # New position - use full target size
                target_shares = int(target_position_value / price)
                logger.debug("New %.1f%% position: %d shares @ $%.2f", position_size, target_shares, price)
                return target_shares, True if side == OrderSide.BUY else False
        except Exception as e:
            logger.info("Error calculating target position %r", ticker, exc_info=e)
            return 0, False

    def check_position_available(self, ticker):
        """Check if position is available to close"""
        try:
            # Get all positions
            positions = self.trading_client.get_all_positions()
            
            # Find this position
            for pos in positions:
                if pos.ticker == ticker:
                    if float(pos.qty_available) == 0:
                        logger.debug(f"Skipping {ticker} - all shares held for orders")
                        return False
                    return True
                    
            logger.debug("Position not found: %r", ticker)
            return False
            
        except Exception as e:
            logger.error("Error checking position %r", ticker, exc_info=e)
            return False
    
    def check_positions(self, ticker_to_price_map):
        for ticker, position in self.positions.items():
            signal = Signal(strategy="stop-loss", ticker=ticker, price=ticker_to_price_map[ticker])
            if position is not None and self.should_close_position(ticker, signal=signal) is True:
                logger.info(f"Detected signal to close position for {ticker}")
                self.close_position(ticker, signal=signal)

    def close_position(self, ticker, signal=None):
        """Close an existing position"""
        if self.is_backtest:
            return self.close_position_backtest(ticker, signal)
        # Skip if already pending close or shares held
        if ticker in self.pending_closes:
            logger.debug("Skipping %r - close order already pending", ticker)
            return None
        
        if not self.check_position_available(ticker):
            return None
            
        try:
            order = self.trading_client.close_position(ticker)
            if order.status == 'accepted':
                self.pending_closes.add(ticker)
                logger.debug("Close order queued: %r", ticker)
                return order
                
        except Exception as e:
            logger.info("\nError closing position in %r:", ticker)
            logger.info("Error type: %r", type(e).__name__)
            logger.info("Error message: %r", str(e))
            return None

    def close_position_backtest(self, ticker, signal):
        """Close a position during backtesting."""
        position = self.positions.get(ticker)
        if not position:
            return

        total_cost = position.qty * signal.price
        self.cash_balance -= total_cost
        position.qty = 0
        position.is_open = False
        del self.positions[ticker] 
        return position

    def get_account_info(self):
        """Get account information"""
        if self.is_backtest:
            return self.get_backtest_account_info()
        else:
            account = self.trading_client.get_account()
            return {
                'equity': float(account.equity),
                'buying_power': float(account.buying_power),
                'initial_margin': float(account.initial_margin),
                'margin_multiplier': float(account.multiplier),
                'daytrading_buying_power': float(account.daytrading_buying_power)
            }
        
    def get_backtest_account_info(self):
        """Get simulated account information during backtesting."""
        return {
            'equity': float(self.equity),
            'buying_power': float(self.cash_balance),
            'initial_margin': 0,
            'margin_multiplier': 1,
            'daytrading_buying_power': float(self.cash_balance)
        }

    def should_close_position(self, ticker, signal):
        """Determine if a position should be closed based on technical analysis"""
        position = self.positions.get(ticker)
        if not position:
            return False
            
        # Get current exposure
        account = self.get_account_info()
        total_exposure = sum(p.get_exposure(float(account['equity'])) 
                           for p in self.positions.values() if p is not None)
        
        # Close if any of these conditions are met:
        reasons = []
        
        # 1. Significant loss
        if position.pl_pct < -0.04:  # -4% stop loss
            reasons.append(f"Stop loss hit: {position.pl_pct:.1%} P&L")
        
        # 2. Technical score moves against position
        technical_score = signal.score
        if technical_score:
            if position.side == OrderSide.BUY and technical_score < 0.4:
                reasons.append(f"Weak technical score for long: {technical_score:.2f}")
            elif position.side == OrderSide.SELL and technical_score > 0.6:
                reasons.append(f"Strong technical score for short: {technical_score:.2f}")
        
        # 3. Momentum moves against position
        if signal.momentum:
            momentum = signal.momentum
            if position.side == OrderSide.BUY and momentum < -0.02:  # -2% momentum for longs
                reasons.append(f"Negative momentum for long: {momentum:.1f}%")
            elif position.side == OrderSide.SELL and momentum > 0.02:  # +2% momentum for shorts
                reasons.append(f"Positive momentum for short: {momentum:.1f}%")
        
        # 4. Over exposure - close weakest positions
        if technical_score and total_exposure > self.max_total_exposure:
            # Close positions with weak technicals when over-exposed
            if (position.side == OrderSide.BUY and technical_score < 0.5) or \
               (position.side == OrderSide.SELL and technical_score > 0.5):
                reasons.append(f"Reducing exposure ({total_exposure:.1%} total)")
        
        # 5. Mediocre performance with significant age
        position_age = (datetime.now() - position.entry_time).days
        if position_age > 5 and abs(position.pl_pct) < 0.01:
            reasons.append(f"Stagnant position after {position_age} days")
        
        if reasons:
            reason_str = ", ".join(reasons)
            logger.debug("Closing %r due to: %r", ticker, reason_str)
            return True
            
        return False
    
    def stats(self):
        return self.get_account_info()


    def update_pending_orders(self):
        """Update list of pending orders, removing executed ones"""
        if self.is_backtest:
            return 
        try:
            # Get all open orders
            orders = self.trading_client.get_orders()
            
            # Clear old pending orders
            self.pending_orders = []
            
            # Only track orders that are still pending
            for order in orders:
                if order.status in ['new', 'accepted', 'pending']:
                    self.pending_orders.append({
                        'ticker': order.symbol,
                        'shares': float(order.qty),
                        'side': order.side,
                        'order_id': order.id
                    })
                    
        except Exception as e:
            logger.info("Error updating orders: %r", str(e))


    def update_positions(self, order=None, show_status=True):
        """Update position tracking with current market data
        Args:
            show_status: Whether to print current portfolio status
        """
        if self.is_backtest:
            return self.update_positions_backtest(order, show_status=show_status)
        try:
            alpaca_positions = self.trading_client.get_all_positions()
            current_tickers = set()
            
            # Update existing positions and add new ones
            for p in alpaca_positions:
                ticker = p.symbol
                current_tickers.add(ticker)
                qty = float(p.qty)
                current_price = float(p.current_price)
                entry_price = float(p.avg_entry_price)
                side = OrderSide.BUY if qty > 0 else OrderSide.SELL
                
                if ticker not in self.positions:
                    # New position
                    self.positions[ticker] = Position(
                        ticker, qty, entry_price, side, 
                        datetime.now()  # Approximate entry time for existing positions
                    )
                
                # Update position data
                pos: Position = self.positions[ticker]
                pos.qty = qty
                pos.entry_price = entry_price
                pos.update_pl(current_price)
            
            # Remove closed positions
            self.positions = {s: p for s, p in self.positions.items() if s in current_tickers}
            
            # Calculate total exposure excluding pending closes
            account = self.get_account_info()
            active_positions = {s: p for s, p in self.positions.items() 
                              if s not in self.pending_closes}
            total_exposure = sum(p.get_exposure(account['equity']) 
                               for p in active_positions.values())
            
            if show_status:
                logger.info("\nCurrent Portfolio Status:")
                logger.info(f"Total Exposure: {total_exposure:.1%}")
                for pos in active_positions.values():
                    exposure = pos.get_exposure(account['equity'])
                    logger.info("%r (%.1f%% exposure)", pos.__str__(), exposure)
                
                if self.pending_closes:
                    logger.info("\nPending Close Orders:")
                    for ticker in self.pending_closes:
                        logger.info("- %r", ticker)
                
                if self.pending_orders:
                    logger.info("\nPending New Orders:")
                    for order in self.pending_orders:
                        logger.info("- %r (%r)", order['ticker'], order['side'])
                
            return self.positions
            
        except Exception as e:
            logger.info("Error updating positions: %r", str(e))
            return {}
    
    def update_positions_backtest(self, order, show_status=False):
        """Update positions for backtesting, recalculating unrealized P&L."""
        
        if order is None:
            return
        else:
            total_cost = float(order['qty']) * float(order['price'])
            if order['side'] == OrderSide.BUY:
                self.cash_balance -= total_cost
                if self.cash_balance < 0:
                    self.cash_balance += total_cost
                    logger.debug("Insufficient funds to buy")
                    return
            elif order['side'] == OrderSide.SELL:
                if order['ticker'] not in self.positions:
                    logger.debug("No position to sell")
                    return
            position = Position(
                order['ticker'], order['qty'], order['price'], order['side'], datetime.now(), order['direction']
            )
            self.positions[position.ticker] = position
            if order['side'] == OrderSide.SELL:
                self.cash_balance += total_cost
                position.is_open = False
                self.positions[position.ticker] = position
            elif order['side'] == OrderSide.BUY:
                self.cash_balance -= total_cost
                position.is_open = True
                self.positions[position.ticker] = position
        
        latest_price = order['price']
        position = self.positions.get(order['ticker'])
        position.update_pl(latest_price)
    

    def update_backtest_account_position_values(self, timestamp, ticker_to_price_mapping):
        for ticker, price in ticker_to_price_mapping.items():
            if ticker in self.positions:
                if self.positions[ticker] is not None:
                    self.positions[ticker].update_pl(price)
                    self.unrealized_pnl += self.positions[ticker].pl

        self.equity = self.cash_balance + self.unrealized_pnl
        if timestamp.minute % 15 == 0:
            logger.info("Backtest Portfolio Status %r: ", timestamp)
            logger.info("Cash Balance: $%.2f", self.cash_balance)
            logger.info("Equity: $%.2f", self.equity)
            logger.info("Unrealized P&L: $%.2f", self.unrealized_pnl)
            open_positions = {s: p for s, p in self.positions.items() if p is not None and p.is_open}
            for ticker, position in open_positions.items():
                logger.info("%r: %r shares @ $%.2f", ticker, position.qty, position.entry_price)