// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "./FlashArb.sol";

// Test doubles for the on-EVM contract test. Not for deployment.

contract MockERC20 {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 a) external { balanceOf[to] += a; }

    function approve(address s, uint256 a) external returns (bool) {
        allowance[msg.sender][s] = a;
        return true;
    }

    function transfer(address to, uint256 a) external returns (bool) {
        balanceOf[msg.sender] -= a;
        balanceOf[to] += a;
        return true;
    }

    function transferFrom(address f, address t, uint256 a) external returns (bool) {
        allowance[f][msg.sender] -= a;
        balanceOf[f] -= a;
        balanceOf[t] += a;
        return true;
    }
}

// Behaves like Balancer's vault: lends, calls back, and reverts unless repaid.
contract MockVault {
    function flashLoan(address recipient, address[] memory tokens, uint256[] memory amounts, bytes memory userData) external {
        MockERC20 t = MockERC20(tokens[0]);
        uint256 pre = t.balanceOf(address(this));
        t.transfer(recipient, amounts[0]);
        uint256[] memory fees = new uint256[](1);
        FlashArb(recipient).receiveFlashLoan(tokens, amounts, fees, userData);
        require(t.balanceOf(address(this)) >= pre, "not repaid");
    }
}

// Fixed-rate router: out = in * num / den.
contract MockRouter {
    uint256 public num;
    uint256 public den;

    constructor(uint256 n, uint256 d) { num = n; den = d; }

    function swapExactTokensForTokens(uint256 amountIn, uint256, address[] calldata path, address to, uint256)
        external returns (uint256[] memory amounts)
    {
        MockERC20(path[0]).transferFrom(msg.sender, address(this), amountIn);
        uint256 out = amountIn * num / den;
        MockERC20(path[1]).transfer(to, out);
        amounts = new uint256[](2);
        amounts[0] = amountIn;
        amounts[1] = out;
    }
}
