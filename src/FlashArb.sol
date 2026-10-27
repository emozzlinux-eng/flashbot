// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
}

interface IBalancerVault {
    function flashLoan(
        address recipient,
        address[] memory tokens,
        uint256[] memory amounts,
        bytes memory userData
    ) external;
}

interface IUniV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

/// @notice Flash-loan arbitrage between two Uniswap-V2-style routers.
/// Borrows `tokenIn` from Balancer (0% fee), swaps tokenIn -> tokenMid on routerBuy,
/// swaps tokenMid -> tokenIn on routerSell, repays, and reverts unless profit >= minProfit.
/// A revert undoes everything, so a bad trade only costs gas.
contract FlashArb {
    address public immutable owner;
    address public immutable vault;
    bool private _active;

    constructor(address _vault) {
        owner = msg.sender;
        vault = _vault;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    function execute(
        address tokenIn,
        uint256 amount,
        address routerBuy,
        address routerSell,
        address tokenMid,
        uint256 minProfit
    ) external onlyOwner {
        address[] memory tokens = new address[](1);
        uint256[] memory amounts = new uint256[](1);
        tokens[0] = tokenIn;
        amounts[0] = amount;
        _active = true;
        IBalancerVault(vault).flashLoan(
            address(this),
            tokens,
            amounts,
            abi.encode(routerBuy, routerSell, tokenMid, minProfit)
        );
        _active = false;
    }

    // Balancer callback
    function receiveFlashLoan(
        address[] memory tokens,
        uint256[] memory amounts,
        uint256[] memory feeAmounts,
        bytes memory userData
    ) external {
        require(msg.sender == vault && _active, "bad caller");
        (address routerBuy, address routerSell, address tokenMid, uint256 minProfit) =
            abi.decode(userData, (address, address, address, uint256));

        address tokenIn = tokens[0];
        uint256 owed = amounts[0] + feeAmounts[0];

        _swap(routerBuy, tokenIn, tokenMid, amounts[0]);
        _swap(routerSell, tokenMid, tokenIn, IERC20(tokenMid).balanceOf(address(this)));

        uint256 bal = IERC20(tokenIn).balanceOf(address(this));
        require(bal >= owed + minProfit, "no profit");
        _safeTransfer(tokenIn, vault, owed);
        // sweep the profit straight to the owner so nothing sits in the contract
        uint256 profit = bal - owed;
        if (profit > 0) _safeTransfer(tokenIn, owner, profit);
    }

    function _swap(address router, address from, address to, uint256 amountIn) internal {
        _safeApprove(from, router, amountIn);
        address[] memory path = new address[](2);
        path[0] = from;
        path[1] = to;
        IUniV2Router(router).swapExactTokensForTokens(amountIn, 0, path, address(this), block.timestamp);
    }

    function withdraw(address token) external onlyOwner {
        _safeTransfer(token, owner, IERC20(token).balanceOf(address(this)));
    }

    // Low-level helpers so tokens that return no value (e.g. USDT) still work.
    function _safeApprove(address token, address spender, uint256 amount) internal {
        (bool ok, bytes memory data) = token.call(abi.encodeWithSelector(0x095ea7b3, spender, amount));
        require(ok && (data.length == 0 || abi.decode(data, (bool))), "approve failed");
    }

    function _safeTransfer(address token, address to, uint256 amount) internal {
        (bool ok, bytes memory data) = token.call(abi.encodeWithSelector(0xa9059cbb, to, amount));
        require(ok && (data.length == 0 || abi.decode(data, (bool))), "transfer failed");
    }
}
