// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.26;

import {MockUSDC} from "./MockUSDC.sol";

/// @notice Test USDC with EIP-2612 permit, domain like Circle FiatTokenV2_2 (name "USD Coin", version "2").
contract MockUSDCPermit is MockUSDC {
    bytes32 public constant PERMIT_TYPEHASH =
        keccak256("Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)");
    mapping(address => uint256) public nonces;

    function version() external pure returns (string memory) {
        return "2";
    }

    function DOMAIN_SEPARATOR() public view returns (bytes32) {
        return keccak256(
            abi.encode(
                keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak256(bytes(name)),
                keccak256("2"),
                block.chainid,
                address(this)
            )
        );
    }

    function permit(address owner, address spender, uint256 value, uint256 deadline, uint8 v, bytes32 r, bytes32 s)
        external
    {
        require(block.timestamp <= deadline, "permit expired");
        bytes32 digest = keccak256(
            abi.encodePacked(
                "\x19\x01",
                DOMAIN_SEPARATOR(),
                keccak256(abi.encode(PERMIT_TYPEHASH, owner, spender, value, nonces[owner]++, deadline))
            )
        );
        address who = ecrecover(digest, v, r, s);
        require(who != address(0) && who == owner, "invalid signature");
        allowance[owner][spender] = value;
    }
}
