// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.26;

/// @dev Fixed address etched with code in setUp, used as the cfVault in non-CCM tests.
address constant CFV_STUB = 0x00000000000000000000000000000000000cF001;

interface ICFReceiverLike {
    function cfReceive(
        uint32 srcChain,
        bytes calldata srcAddress,
        bytes calldata message,
        address token,
        uint256 amount
    ) external payable;
}

interface IERC20Like {
    function transfer(address to, uint256 amount) external returns (bool);
}

/// @notice Mimics Chainflip Vault._executexSwapAndCall: transfer first (ERC-20) or attach value (native),
///         then call cfReceive. A revert in the receiver bubbles up, like the real Vault (whose
///         broadcast would then fail and the funds would stay in the Vault).
contract MockCfVault {
    address internal constant NATIVE = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE;

    receive() external payable {}

    struct TransferParams {
        address token;
        address recipient;
        uint256 amount;
    }

    function executexSwapAndCall(
        TransferParams calldata t,
        uint32 srcChain,
        bytes calldata srcAddress,
        bytes calldata message
    ) external {
        uint256 nativeAmount;
        if (t.amount > 0) {
            if (t.token == NATIVE) nativeAmount = t.amount;
            else require(IERC20Like(t.token).transfer(t.recipient, t.amount), "transfer");
        }
        ICFReceiverLike(t.recipient).cfReceive{value: nativeAmount}(srcChain, srcAddress, message, t.token, t.amount);
    }
}
