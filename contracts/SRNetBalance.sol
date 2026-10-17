// SPDX-License-Identifier: Apache-2.0
pragma solidity 0.8.26;

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @dev EIP-2612 (Circle native USDC / FiatTokenV2_2 implements it).
interface IERC20Permit {
    function permit(address owner, address spender, uint256 value, uint256 deadline, uint8 v, bytes32 r, bytes32 s)
        external;
}

interface IERC20Allowance {
    function allowance(address owner, address spender) external view returns (uint256);
}

interface IERC20Metadata {
    function decimals() external view returns (uint8);
    function symbol() external view returns (string memory);
}

/// @title SRNetBalance: prepaid hourly exit leases, final on start, with Chainflip CCM top-up and gasless signed actions
/// @notice USDC prepaid balance for SRNet exit leases. No disputes, no judge, no operator. All lease risk is on the
///         buyer: buy one hour at a time (the client renews hourly) so the most a buyer can lose is ~1 hour.
///
///   1. Credit arrives by `fund` (USDC transferFrom) or by Chainflip CCM (`cfReceive`). A fee fixed at deploy
///      (<= 3%) is taken once, at deposit. Nothing else is ever charged by the protocol.
///   2. Leases are prepaid whole HOURS at the seller's listed hourly rate: `startLease(vendorId, ratePerHour,
///      hours)` (hours >= 1, <= maxLeaseHours; rate * hours <= maxPerLease). `renewLease(prevId, hours)` buys
///      more hours from the same seller at the same rate, starting exactly when the previous lease ends.
///   3. Payment is FINAL once a lease starts. The buyer cannot cancel and gets no refund.
///   4. Each hour becomes the seller's the moment that hour begins (`earned` = rate * hours begun). The seller
///      may `claimEarned` at any time (so a 1-hour lease is claimable immediately); after the end anyone may
///      `finalize` and the seller gets everything.
///   5. Only the SELLER may `cancel` (stop serving early). The seller keeps the hours already begun and the
///      buyer gets every unused whole hour back. A lease the seller stopped cannot be renewed.
///   6. Sellers and relayers withdraw their payable with `withdrawPayable` (`withdrawVendor` is the same
///      function under its original name), buyers `cashOut`/`withdrawFor`, anyone `withdrawFees`.
///      Relay fees are credited to the relayer's `payableOf`, the same balance sellers are paid into.
///   7. Gasless: a user signs EIP-712 StartLease / Renew / Withdraw; ANYONE may submit it and is paid the
///      user-signed `relayFee` (<= immutable `maxRelayFee`) from the user's available balance.
///   8. CCM: `cfReceive` is callable ONLY by the immutable Chainflip Vault and never reverts for it. USDC with
///      an unreadable message is HELD (never fees): claimable by a 20-byte srcAddress (EVM-style source),
///      otherwise locked permanently in `unattributedUsdc` -- nobody, including the owner, can move it.
///
/// Owner powers (complete list): two-step ownership handover, set fee recipient, pause new deposits and new
/// leases, rescue tokens that are NOT USDC (and never amounts held for users), and sweep USDC surplus above
/// `totalLiabilities` to the fee recipient. The owner cannot move, freeze, or redirect any USDC owed to
/// anyone, cannot cancel leases, cannot change the fee, caps, rules or Vault, cannot upgrade.
///
/// Vault and token are immutable. If Chainflip moves its Vault, CCM deposits stop arriving here and the fix
/// is a new contract; users cash out of this one (`fund`, leases and withdrawals keep working).
///
/// Invariant: usdc.balanceOf(this) >= totalLiabilities == sum(availableOf) + sum(lockedOf) + sum(payableOf)
///            + accruedFees + heldTotal[usdc] + unattributedUsdc
/// The difference (`surplusUsdc()`) is USDC sent here directly, outside `fund`/`cfReceive`. It is owed to
/// nobody. `cfReceive` books the amount the Vault reports for that call, clipped to the surplus at that
/// moment; with an honest Vault the reported amount is exactly what it just transferred, so an earlier
/// direct transfer is not absorbed. Only a Vault over-report could book it. `sweepSurplus` sends it to
/// the fee recipient and can never touch liabilities.
contract SRNetBalance {
    uint256 public constant MAX_FEE_BPS = 300;
    uint256 public constant BPS_DENOMINATOR = 10_000;
    uint256 public constant HOUR = 1 hours;
    uint256 public constant MAX_LEASE_HOURS_LIMIT = 720; // 30 days
    uint8 public constant TOKEN_DECIMALS = 6;
    /// @notice Chainflip's marker for the chain's native token in `cfReceive(..., token, ...)`.
    address public constant NATIVE = 0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE;
    /// @notice CCM message format version byte.
    uint8 public constant CCM_MSG_VERSION = 1;
    /// @notice Hard ceiling for the optional USDC "wallet float" a CCM message may ask to forward
    ///         to the beneficiary's own wallet (e.g. to pay gas through a USDC paymaster). $5.
    uint256 public constant MAX_WALLET_FLOAT = 5e6;
    /// @notice Hard ceiling for `maxRelayFee` chosen at deploy. $5.
    uint256 public constant MAX_RELAY_FEE_LIMIT = 5e6;
    /// @notice Gas forwarded to each best-effort push inside `cfReceive`.
    uint256 public constant PUSH_GAS = 100_000;

    bytes32 public constant START_LEASE_TYPEHASH = keccak256(
        "StartLease(address buyer,bytes32 vendorId,uint256 ratePerHour,uint256 hoursBought,uint256 relayFee,address relayer,uint256 nonce,uint256 deadline)"
    );
    bytes32 public constant RENEW_TYPEHASH = keccak256(
        "Renew(address buyer,uint256 prevLeaseId,uint256 hoursBought,uint256 relayFee,address relayer,uint256 nonce,uint256 deadline)"
    );
    bytes32 public constant WITHDRAW_TYPEHASH = keccak256(
        "Withdraw(address user,uint256 amount,address to,uint256 relayFee,address relayer,uint256 nonce,uint256 deadline)"
    );
    /// @notice Gasless deposit intent: `owner` lets the contract pull `amount` USDC from their wallet and
    ///         credit `beneficiary`, paying at most `relayFee` (USDC, from the deposit) to the submitter.
    bytes32 public constant FUND_TYPEHASH = keccak256(
        "Fund(address owner,address beneficiary,uint256 amount,uint256 relayFee,address relayer,uint256 nonce,uint256 deadline)"
    );
    bytes32 private constant _DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 private constant _NAME_HASH = keccak256("SRNetBalance");
    bytes32 private constant _VERSION_HASH = keccak256("1");
    uint256 private constant _HALF_N = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0;

    /// @notice Settlement token (USDC). Validated at construction: has code, 6 decimals, symbol "USDC".
    IERC20 public immutable usdc;
    /// @notice Fee on `fund`, in bps. Fixed at deploy (<= 300).
    uint256 public immutable feeBps;
    /// @notice Max USDC (base units) per lease, fixed at deploy (e.g. 20e6 = $20).
    uint256 public immutable maxPerLease;
    /// @notice Max whole hours per lease (<= 720).
    uint256 public immutable maxLeaseHours;
    /// @notice The only address allowed to call `cfReceive` (Chainflip Vault on this chain). Immutable:
    ///         if Chainflip ever migrates its Vault, deploy a new SRNetBalance.
    address public immutable cfVault;
    /// @notice Upper bound on any user-signed relay fee (USDC base units).
    uint256 public immutable maxRelayFee;
    uint256 private immutable _cachedChainId;
    bytes32 private immutable _cachedDomainSeparator;

    address public owner;
    address public pendingOwner;
    address public feeRecipient;
    uint256 public nextLeaseId;
    /// @notice Emergency lever: when set, `fund`, `startLease`/`renewLease` (and signed versions) revert, and
    ///         CCM USDC is passed straight to the beneficiary's wallet. Running leases, seller cancel,
    ///         finalize, claims and all withdrawals are never affected.
    bool public paused;
    /// @notice Fees taken at `fund`, not yet sent to `feeRecipient`.
    uint256 public accruedFees;
    /// @notice Sum of every USDC amount this contract owes: available + locked + payable + accruedFees
    ///         + heldTotal[usdc] + unattributedUsdc. `cfReceive` never credits more than usdc.balanceOf(this) - this, so even
    ///         a compromised or buggy Vault cannot mint credit that was not actually delivered.
    uint256 public totalLiabilities;
    /// @notice CCM USDC with an unreadable message and no claimable srcAddress. Permanently locked: no
    ///         function moves it (no owner discretion over user money). See `cfReceive`.
    uint256 public unattributedUsdc;

    /// @dev Four slots. amount <= maxPerLease fits uint96; duration (hours * 3600, <= 30 days) fits uint32.
    struct Lease {
        address buyer;
        uint64 start; // may be in the future for a renewal (starts when the previous lease ends)
        uint32 duration;
        address sellerWallet;
        uint96 amount; // ratePerHour * hours, prepaid
        uint96 credited; // already moved to the seller's payable
        uint64 endedAt; // 0 while open; end of paid time once closed
        bool closed;
        bool sellerStopped; // closed early by the seller (unused hours refunded)
        bytes32 vendorId;
    }

    mapping(bytes32 vendorId => address wallet) public vendorWallet;
    mapping(address vendorWallet => uint256 amount) public payableOf;
    mapping(address user => uint256 amount) public availableOf;
    /// @notice USDC locked in this buyer's open leases and not yet credited to sellers.
    mapping(address user => uint256 amount) public lockedOf;
    mapping(uint256 leaseId => Lease) private _leases;
    /// @notice CCM proceeds that could not be pushed or credited, claimable only by `user`.
    mapping(address token => mapping(address user => uint256 amount)) public heldOf;
    mapping(address token => uint256 amount) public heldTotal;
    /// @notice Unordered nonces for signed actions.
    mapping(address user => mapping(uint256 nonce => bool used)) public nonceUsed;
    /// @notice The lease that renewed `leaseId` (0 if none). Each lease can be renewed once, so a retried
    ///         or duplicated renewal can never make a buyer pay twice for the same hours.
    mapping(uint256 leaseId => uint256 nextId) public renewedBy;

    struct StartLeaseAuth {
        address buyer;
        bytes32 vendorId;
        uint256 ratePerHour;
        uint256 hoursBought;
        uint256 relayFee;
        address relayer; // address(0) = anyone may submit
        uint256 nonce;
        uint256 deadline;
    }

    struct RenewAuth {
        address buyer;
        uint256 prevLeaseId;
        uint256 hoursBought;
        uint256 relayFee;
        address relayer;
        uint256 nonce;
        uint256 deadline;
    }

    struct WithdrawAuth {
        address user;
        uint256 amount;
        address to;
        uint256 relayFee;
        address relayer;
        uint256 nonce;
        uint256 deadline;
    }

    struct FundAuth {
        address owner; // wallet the USDC is pulled from (signs this intent and the permit)
        address beneficiary; // whose SRNet balance is credited (normally == owner)
        uint256 amount; // USDC pulled from owner, base units
        uint256 relayFee; // paid to msg.sender out of the deposit (<= maxRelayFee)
        address relayer; // address(0) = anyone may submit
        uint256 nonce;
        uint256 deadline;
    }

    /// @notice EIP-2612 permit signature. `deadline == 0` means "no permit, use the existing allowance".
    struct PermitSig {
        uint256 value;
        uint256 deadline;
        uint8 v;
        bytes32 r;
        bytes32 s;
    }

    uint256 private _lock = 1;

    event Funded(address indexed user, uint256 amount, uint256 fee, uint256 credited);
    event FeeRecipientUpdated(address indexed previous, address indexed next);
    event FeesWithdrawn(address indexed to, uint256 amount);
    /// @notice USDC surplus above totalLiabilities sent to the fee recipient.
    event SurplusSwept(address indexed to, uint256 amount);
    event PauseSet(bool paused);
    event OwnershipTransferStarted(address indexed previousOwner, address indexed newOwner);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event VendorConfigured(bytes32 indexed vendorId, address indexed wallet);
    event VendorWalletChanged(bytes32 indexed vendorId, address indexed wallet);
    event LeaseStarted(
        uint256 indexed leaseId, address indexed buyer, uint256 ratePerHour, uint256 hoursBought, uint256 amount
    );
    /// @notice A renewal: `leaseId` continues `prevLeaseId` and starts at `start` (the previous end, or now).
    event LeaseRenewed(uint256 indexed leaseId, uint256 indexed prevLeaseId, uint256 start);
    /// @notice `by` is the seller wallet that stopped the lease, or address(0) for a natural end.
    event LeaseClosed(uint256 indexed leaseId, address indexed by, uint256 toSeller, uint256 toBuyer);
    /// @dev Vendor wallet, amount, and lease id. No buyer.
    event PayableCredited(address indexed vendorWallet, uint256 amount, uint256 indexed leaseId);
    event HoldReturned(uint256 indexed leaseId, address indexed user, uint256 amount);
    event VendorWithdrawn(address indexed vendorWallet, uint256 amount);
    event CashedOut(address indexed user, uint256 amount);
    event TokenRescued(address indexed token, address indexed to, uint256 amount);
    /// @notice USDC arrived through Chainflip CCM and was credited to `beneficiary` (fee taken as in fund).
    event CcmCredited(
        address indexed beneficiary, uint32 srcChain, uint256 amount, uint256 fee, uint256 credited, uint256 walletFloat
    );
    /// @notice CCM proceeds were forwarded straight to `to` (non-USDC token, paused, or wallet float).
    event CcmForwarded(address indexed to, address indexed token, uint256 amount);
    /// @notice A best-effort forward failed; the amount is now in heldOf[token][user].
    event CcmHeld(address indexed user, address indexed token, uint256 amount);
    /// @notice USDC arrived with a message we cannot attribute. `claimant` != 0: held for that 20-byte
    ///         srcAddress (claimHeld). `claimant` == 0: added to the permanently locked `unattributedUsdc`.
    event CcmUnattributed(
        uint32 srcChain, bytes srcAddress, bytes message, uint256 amount, address indexed claimant
    );
    /// @notice Non-USDC proceeds arrived with an unattributable message; left untracked (owner-rescuable).
    event CcmUntracked(address indexed token, uint256 amount, bytes message);
    /// @notice The caller reported more USDC than arrived; only the real surplus was booked.
    event CcmAmountClipped(uint256 reported, uint256 booked);
    event HeldClaimed(address indexed user, address indexed token, uint256 amount);
    event RelayFeePaid(address indexed user, address indexed relayer, uint256 amount);
    event NonceInvalidated(address indexed user, uint256 nonce);

    error ZeroAmount();
    error ZeroAddress();
    error NotOwner();
    error NotPendingOwner();
    error Paused();
    error LeaseTooLarge();
    error LeaseNotOpen();
    error NotLeaseSeller();
    error LeaseNotOver();
    error FeeBpsTooHigh();
    error InsufficientAvailable();
    error InsufficientPayable();
    error VendorNotConfigured();
    error VendorAlreadyConfigured();
    error NotVendorWallet();
    error SameWallet();
    error ZeroHours();
    error TooManyHours();
    error ZeroRate();
    error BadDeployCap();
    error BadToken();
    error CannotRescueUsdc();
    error UsdcCallFailed();
    error UsdcReturnedFalse();
    error TransferAmountMismatch();
    error Reentered();
    error EthRejected();
    error VendorIdMismatch();
    error FeeRecipientIsSelf();
    error NotCfVault();
    error BadVault();
    error RelayFeeTooHigh();
    error SignatureExpired();
    error NonceAlreadyUsed();
    error BadSignature();
    error WrongRelayer();
    error NothingHeld();
    error NativeSendFailed();
    error ExceedsUntracked();
    error NotLeaseBuyer();
    error RenewNotAllowed();
    error RenewTooEarly();
    error AlreadyRenewed();
    error InsufficientAllowance();

    modifier nonReentrant() {
        if (_lock != 1) revert Reentered();
        _lock = 2;
        _;
        _lock = 1;
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier whenNotPaused() {
        if (paused) revert Paused();
        _;
    }

    /// @param usdc_ USDC token. Must have code, decimals() == 6 and symbol() == "USDC".
    /// @param feeRecipient_ initial fee recipient (owner may change it).
    /// @param owner_ initial owner (two-step handover).
    /// @param maxPerLease_ max USDC per lease (e.g. 20e6 = $20).
    /// @param maxLeaseHours_ max whole hours per lease (<= 720; e.g. 168 = 7 days).
    /// @param feeBps_ fee on deposits in bps (<= 300). Immutable.
    /// @param cfVault_ Chainflip Vault on this chain (must have code). Immutable.
    /// @param maxRelayFee_ cap on any signed relay fee, USDC base units (<= 5e6). Immutable.
    constructor(
        address usdc_,
        address feeRecipient_,
        address owner_,
        uint256 maxPerLease_,
        uint256 maxLeaseHours_,
        uint256 feeBps_,
        address cfVault_,
        uint256 maxRelayFee_
    ) {
        if (usdc_ == address(0) || feeRecipient_ == address(0) || owner_ == address(0)) {
            revert ZeroAddress();
        }
        if (feeRecipient_ == address(this)) revert FeeRecipientIsSelf();
        if (maxPerLease_ == 0 || maxPerLease_ > type(uint96).max) revert BadDeployCap();
        if (maxLeaseHours_ == 0 || maxLeaseHours_ > MAX_LEASE_HOURS_LIMIT) revert BadDeployCap();
        if (feeBps_ > MAX_FEE_BPS) revert FeeBpsTooHigh();
        if (cfVault_ == address(0) || cfVault_.code.length == 0 || cfVault_ == usdc_) revert BadVault();
        if (maxRelayFee_ > MAX_RELAY_FEE_LIMIT) revert RelayFeeTooHigh();
        _validateToken(usdc_);
        cfVault = cfVault_;
        maxRelayFee = maxRelayFee_;
        _cachedChainId = block.chainid;
        _cachedDomainSeparator = _buildDomainSeparator();
        usdc = IERC20(usdc_);
        feeRecipient = feeRecipient_;
        owner = owner_;
        maxPerLease = maxPerLease_;
        maxLeaseHours = maxLeaseHours_;
        feeBps = feeBps_;
        emit OwnershipTransferred(address(0), owner_);
        emit FeeRecipientUpdated(address(0), feeRecipient_);
    }

    receive() external payable {
        revert EthRejected();
    }

    fallback() external payable {
        revert EthRejected();
    }

    /// @notice The settlement token address (same as `usdc`).
    function token() external view returns (address) {
        return address(usdc);
    }

    // ── buyers ────────────────────────────────────────────────────────────────

    /// @notice Pull USDC from the caller. The fee accrues now; the rest is credited as available.
    ///         The fee is not returned on cashOut. Blocked while paused.
    function fund(uint256 usdcAmount) external nonReentrant whenNotPaused {
        if (usdcAmount == 0) revert ZeroAmount();
        uint256 fee = (usdcAmount * feeBps) / BPS_DENOMINATOR;
        uint256 credited = usdcAmount - fee;
        uint256 before = usdc.balanceOf(address(this));
        _call(address(usdc), abi.encodeCall(IERC20.transferFrom, (msg.sender, address(this), usdcAmount)));
        if (usdc.balanceOf(address(this)) - before != usdcAmount) revert TransferAmountMismatch();
        accruedFees += fee;
        availableOf[msg.sender] += credited;
        totalLiabilities += usdcAmount;
        emit Funded(msg.sender, usdcAmount, fee, credited);
    }

    /// @notice Buy `hoursBought` prepaid whole hours at `ratePerHour` (USDC base units) starting now.
    ///         Locks ratePerHour * hoursBought (<= maxPerLease). FINAL: the buyer cannot cancel. Paused: reverts.
    function startLease(bytes32 vendorId, uint256 ratePerHour, uint256 hoursBought)
        external
        whenNotPaused
        returns (uint256 leaseId)
    {
        return _startLease(msg.sender, vendorId, ratePerHour, hoursBought, block.timestamp);
    }

    /// @notice Buy `hoursBought` more hours from the same seller at the same rate as `prevLeaseId`, starting
    ///         when it ends (or now if it already ended). Allowed in the previous lease's last hour or later,
    ///         never after the seller stopped it. FINAL like startLease. Paused: reverts.
    function renewLease(uint256 prevLeaseId, uint256 hoursBought) external whenNotPaused returns (uint256 leaseId) {
        return _renew(msg.sender, prevLeaseId, hoursBought);
    }

    function _renew(address buyer, uint256 prevLeaseId, uint256 hoursBought) private returns (uint256 leaseId) {
        Lease storage p = _leases[prevLeaseId];
        if (p.buyer == address(0)) revert LeaseNotOpen();
        if (p.buyer != buyer) revert NotLeaseBuyer();
        if (p.sellerStopped) revert RenewNotAllowed();
        uint256 prevEnd = uint256(p.start) + p.duration;
        if (prevEnd > block.timestamp + HOUR) revert RenewTooEarly();
        if (renewedBy[prevLeaseId] != 0) revert AlreadyRenewed();
        // Exact: _startLease stores amount = rate * hours and duration = hours * HOUR, so no rounding.
        uint256 rate = uint256(p.amount) / (uint256(p.duration) / HOUR);
        uint256 start = prevEnd > block.timestamp ? prevEnd : block.timestamp;
        leaseId = _startLease(buyer, p.vendorId, rate, hoursBought, start);
        renewedBy[prevLeaseId] = leaseId;
        emit LeaseRenewed(leaseId, prevLeaseId, start);
    }

    function _startLease(address buyer, bytes32 vendorId, uint256 ratePerHour, uint256 hoursBought, uint256 start)
        private
        returns (uint256 leaseId)
    {
        if (ratePerHour == 0) revert ZeroRate();
        if (hoursBought == 0) revert ZeroHours();
        if (hoursBought > maxLeaseHours) revert TooManyHours();
        if (ratePerHour > maxPerLease) revert LeaseTooLarge();
        uint256 amount = ratePerHour * hoursBought;
        if (amount > maxPerLease) revert LeaseTooLarge();
        uint256 duration = hoursBought * HOUR;
        address wallet = vendorWallet[vendorId];
        if (wallet == address(0)) revert VendorNotConfigured();
        uint256 bal = availableOf[buyer];
        if (bal < amount) revert InsufficientAvailable();
        availableOf[buyer] = bal - amount;
        lockedOf[buyer] += amount;
        leaseId = ++nextLeaseId;
        _leases[leaseId] = Lease({
            buyer: buyer,
            start: uint64(start),
            duration: uint32(duration),
            sellerWallet: wallet,
            amount: uint96(amount),
            credited: 0,
            endedAt: 0,
            closed: false,
            sellerStopped: false,
            vendorId: vendorId
        });
        emit LeaseStarted(leaseId, buyer, ratePerHour, hoursBought, amount);
    }

    /// @notice SELLER stops an open lease. The seller keeps every hour already begun; the buyer gets the
    ///         unused whole hours back. The buyer cannot cancel (payment is final). Works while paused.
    ///         After the end time it behaves like `finalize`.
    function cancel(uint256 leaseId) external nonReentrant {
        Lease storage l = _leases[leaseId];
        if (l.buyer == address(0) || l.closed) revert LeaseNotOpen();
        if (msg.sender != l.sellerWallet) revert NotLeaseSeller();
        _close(leaseId, l, msg.sender);
    }

    /// @notice After a lease's full duration, anyone may close it: the seller gets the whole amount.
    function finalize(uint256 leaseId) external nonReentrant {
        Lease storage l = _leases[leaseId];
        if (l.buyer == address(0) || l.closed) revert LeaseNotOpen();
        if (block.timestamp < uint256(l.start) + l.duration) revert LeaseNotOver();
        _close(leaseId, l, address(0));
    }

    /// @notice Seller moves every hour begun so far on an open lease into its payable.
    function claimEarned(uint256 leaseId) external nonReentrant returns (uint256 credited) {
        Lease storage l = _leases[leaseId];
        if (l.buyer == address(0) || l.closed) revert LeaseNotOpen();
        if (msg.sender != l.sellerWallet) revert NotLeaseSeller();
        credited = _earned(l) - l.credited;
        if (credited != 0) _creditSeller(leaseId, l, credited);
    }

    /// @notice Seller's share of `leaseId` as of now: rate * hours begun (whole hours).
    function earned(uint256 leaseId) external view returns (uint256) {
        Lease storage l = _leases[leaseId];
        if (l.closed) return l.credited;
        return _earned(l);
    }

    /// @notice Withdraw unused available balance to yourself. Never paused. Fee not refunded.
    function cashOut(uint256 amount) external nonReentrant {
        _cashOut(msg.sender, amount, msg.sender);
    }

    function _cashOut(address user, uint256 amount, address to) private {
        if (amount == 0) revert ZeroAmount();
        if (to == address(0)) revert ZeroAddress();
        uint256 bal = availableOf[user];
        if (bal < amount) revert InsufficientAvailable();
        availableOf[user] = bal - amount;
        totalLiabilities -= amount;
        _transfer(to, amount);
        emit CashedOut(user, amount);
    }

    // ── Chainflip CCM top-up ──────────────────────────────────────────────────

    /// @notice Chainflip CCM entry point. The Vault has ALREADY transferred `amount` of `token` (or sent
    ///         msg.value for native) before calling. Only the immutable `cfVault` may call.
    ///
    ///   message (packed): uint8 version(=1) | address beneficiary            (21 bytes), or
    ///                     uint8 version(=1) | address beneficiary | uint64 walletFloat (29 bytes)
    ///   srcAddress: Chainflip passes it EMPTY for deposit-channel swaps; for Vault swaps from an EVM chain it
    ///   is the 20-byte sender. It is used ONLY to make unreadable-message USDC claimable (below).
    ///
    ///   Branches (none reverts for a Vault call, because a revert strands the funds in the Vault):
    ///   - USDC, valid message, not paused: walletFloat (<= MAX_WALLET_FLOAT, <= amount) is pushed to the
    ///     beneficiary's wallet (fee-free); the rest is credited like `fund` (fee taken). A failed push is
    ///     credited instead.
    ///   - USDC, valid message, paused: whole amount pushed to the beneficiary's wallet, fee-free;
    ///     if that push fails, it is held for them (`claimHeld`). Nothing enters lease accounting.
    ///   - USDC, unattributable message: HELD. If srcAddress is exactly 20 bytes, held for that address
    ///     (`claimHeld(usdc)`); otherwise added to `unattributedUsdc`, which no function can move.
    ///   - Native / other token, valid message: pushed to the beneficiary; on failure held for them.
    ///   - Native / other token, unattributable: left untracked; owner may rescue (never USDC).
    function cfReceive(
        uint32 srcChain,
        bytes calldata srcAddress,
        bytes calldata message,
        address token_,
        uint256 amount
    ) external payable nonReentrant {
        if (msg.sender != cfVault) revert NotCfVault();
        (bool ok, address beneficiary, uint256 walletFloat) = _decodeCcm(message);
        if (token_ == NATIVE) amount = msg.value; // trust msg.value over the parameter for native
        if (amount == 0) return;

        if (token_ == address(usdc)) {
            // Never book more than actually arrived (defence against a faulty/compromised caller).
            uint256 bal = usdc.balanceOf(address(this));
            uint256 surplus = bal > totalLiabilities ? bal - totalLiabilities : 0;
            if (amount > surplus) {
                emit CcmAmountClipped(amount, surplus);
                amount = surplus;
                if (amount == 0) return;
            }
            if (!ok) {
                address claimant = _evmSrc(srcAddress);
                totalLiabilities += amount;
                if (claimant != address(0)) {
                    heldOf[address(usdc)][claimant] += amount;
                    heldTotal[address(usdc)] += amount;
                } else {
                    unattributedUsdc += amount;
                }
                emit CcmUnattributed(srcChain, srcAddress, message, amount, claimant);
                return;
            }
            if (paused) {
                _forwardOrHold(address(usdc), beneficiary, amount);
                return;
            }
            _creditCcm(beneficiary, srcChain, amount, walletFloat);
            return;
        }

        if (!ok) {
            address claimant = _evmSrc(srcAddress);
            if (claimant != address(0)) {
                heldOf[token_][claimant] += amount;
                heldTotal[token_] += amount;
                emit CcmHeld(claimant, token_, amount);
            } else {
                emit CcmUntracked(token_, amount, message);
            }
            return;
        }
        _forwardOrHold(token_, beneficiary, amount);
    }

    /// @notice Claim CCM proceeds held for you (USDC, native, or other token). Never paused.
    function claimHeld(address tokenAddr) external nonReentrant {
        uint256 amount = heldOf[tokenAddr][msg.sender];
        if (amount == 0) revert NothingHeld();
        heldOf[tokenAddr][msg.sender] = 0;
        heldTotal[tokenAddr] -= amount;
        if (tokenAddr == address(usdc)) totalLiabilities -= amount;
        if (tokenAddr == NATIVE) {
            (bool sent,) = msg.sender.call{value: amount}("");
            if (!sent) revert NativeSendFailed();
        } else {
            _call(tokenAddr, abi.encodeCall(IERC20.transfer, (msg.sender, amount)));
        }
        emit HeldClaimed(msg.sender, tokenAddr, amount);
    }

    /// @notice Decode a CCM message exactly as `cfReceive` will. Clients MUST check this before opening a
    ///         Chainflip channel: an unreadable message locks USDC permanently (deposit channels).
    function decodeCcmMessage(bytes calldata message)
        external
        view
        returns (bool ok, address beneficiary, uint256 walletFloat)
    {
        return _decodeCcm(message);
    }

    /// @notice Build the CCM message for `beneficiary` (helper for clients/tests).
    function encodeCcmMessage(address beneficiary, uint64 walletFloat) external pure returns (bytes memory) {
        if (walletFloat == 0) return abi.encodePacked(CCM_MSG_VERSION, beneficiary);
        return abi.encodePacked(CCM_MSG_VERSION, beneficiary, walletFloat);
    }

    // ── gasless signed actions (anyone may relay; relayer paid in USDC) ──────

    /// @notice Submit a buyer-signed StartLease. Lease amount + relayFee come from the buyer's available.
    function startLeaseFor(StartLeaseAuth calldata a, bytes calldata sig)
        external
        whenNotPaused
        returns (uint256 leaseId)
    {
        bytes32 structHash = keccak256(
            abi.encode(
                START_LEASE_TYPEHASH,
                a.buyer,
                a.vendorId,
                a.ratePerHour,
                a.hoursBought,
                a.relayFee,
                a.relayer,
                a.nonce,
                a.deadline
            )
        );
        _useAuth(a.buyer, structHash, a.relayFee, a.relayer, a.nonce, a.deadline, sig);
        leaseId = _startLease(a.buyer, a.vendorId, a.ratePerHour, a.hoursBought, block.timestamp);
        _payRelayer(a.buyer, a.relayFee);
    }

    /// @notice Submit a buyer-signed Renew (more hours, same seller and rate as `prevLeaseId`).
    function renewFor(RenewAuth calldata a, bytes calldata sig) external whenNotPaused returns (uint256 leaseId) {
        bytes32 structHash = keccak256(
            abi.encode(
                RENEW_TYPEHASH, a.buyer, a.prevLeaseId, a.hoursBought, a.relayFee, a.relayer, a.nonce, a.deadline
            )
        );
        _useAuth(a.buyer, structHash, a.relayFee, a.relayer, a.nonce, a.deadline, sig);
        leaseId = _renew(a.buyer, a.prevLeaseId, a.hoursBought);
        _payRelayer(a.buyer, a.relayFee);
    }

    /// @notice Submit a user-signed Withdraw of `amount` to `to`; relayFee is taken on top. Never paused.
    function withdrawFor(WithdrawAuth calldata a, bytes calldata sig) external nonReentrant {
        bytes32 structHash = keccak256(
            abi.encode(WITHDRAW_TYPEHASH, a.user, a.amount, a.to, a.relayFee, a.relayer, a.nonce, a.deadline)
        );
        _useAuth(a.user, structHash, a.relayFee, a.relayer, a.nonce, a.deadline, sig);
        _payRelayer(a.user, a.relayFee);
        _cashOut(a.user, a.amount, a.to);
    }

    /// @notice Gasless deposit from a phone-funded wallet that holds USDC but no ETH.
    ///         1. Best-effort `permit` (a front-runner submitting the same permit first cannot block this:
    ///            the failure is ignored and the allowance it created is used).
    ///         2. Pull exactly `a.amount` from `a.owner`. Protocol fee (feeBps) is taken on `a.amount` as in
    ///            `fund`; `a.relayFee` goes to msg.sender's payable; the rest is available to `a.beneficiary`.
    ///         The intent signature binds owner, beneficiary, amount, fee cap, relayer, nonce and deadline.
    function fundWithPermit(FundAuth calldata a, bytes calldata sig, PermitSig calldata p)
        external
        nonReentrant
        whenNotPaused
    {
        if (a.amount == 0) revert ZeroAmount();
        if (a.beneficiary == address(0)) revert ZeroAddress();
        bytes32 structHash = keccak256(
            abi.encode(
                FUND_TYPEHASH, a.owner, a.beneficiary, a.amount, a.relayFee, a.relayer, a.nonce, a.deadline
            )
        );
        _useAuth(a.owner, structHash, a.relayFee, a.relayer, a.nonce, a.deadline, sig);
        if (p.deadline != 0) {
            try IERC20Permit(address(usdc)).permit(a.owner, address(this), p.value, p.deadline, p.v, p.r, p.s) {}
                catch {}
        }
        if (IERC20Allowance(address(usdc)).allowance(a.owner, address(this)) < a.amount) {
            revert InsufficientAllowance();
        }
        uint256 fee = (a.amount * feeBps) / BPS_DENOMINATOR;
        uint256 credited = a.amount - fee;
        if (a.relayFee > credited) revert RelayFeeTooHigh();
        uint256 before = usdc.balanceOf(address(this));
        _call(address(usdc), abi.encodeCall(IERC20.transferFrom, (a.owner, address(this), a.amount)));
        if (usdc.balanceOf(address(this)) - before != a.amount) revert TransferAmountMismatch();
        accruedFees += fee;
        credited -= a.relayFee;
        availableOf[a.beneficiary] += credited;
        totalLiabilities += a.amount;
        if (a.relayFee != 0) {
            payableOf[msg.sender] += a.relayFee;
            emit RelayFeePaid(a.owner, msg.sender, a.relayFee);
        }
        emit Funded(a.beneficiary, a.amount, fee, credited);
    }

    /// @notice Burn one of your own nonces so a signed action can never be submitted.
    function invalidateNonce(uint256 nonce) external {
        nonceUsed[msg.sender][nonce] = true;
        emit NonceInvalidated(msg.sender, nonce);
    }

    /// @notice EIP-712 domain separator (name "SRNetBalance", version "1").
    function DOMAIN_SEPARATOR() public view returns (bytes32) {
        return block.chainid == _cachedChainId ? _cachedDomainSeparator : _buildDomainSeparator();
    }

    /// @notice EIP-712 digest a user signs for a given struct hash.
    function hashTypedData(bytes32 structHash) public view returns (bytes32) {
        return keccak256(abi.encodePacked("\x19\x01", DOMAIN_SEPARATOR(), structHash));
    }

    // ── sellers ───────────────────────────────────────────────────────────────

    /// @notice Vendor binds their own wallet, once. `vendorId` must be `keccak256(abi.encodePacked(msg.sender, salt))`.
    function configureVendor(bytes32 vendorId, bytes32 salt) external {
        if (vendorId != keccak256(abi.encodePacked(msg.sender, salt))) revert VendorIdMismatch();
        if (vendorWallet[vendorId] != address(0)) revert VendorAlreadyConfigured();
        vendorWallet[vendorId] = msg.sender;
        emit VendorConfigured(vendorId, msg.sender);
    }

    /// @notice Current vendor wallet only. Affects future leases; open leases and existing payable keep the old wallet.
    function changeVendorWallet(bytes32 vendorId, address newWallet) external {
        if (newWallet == address(0)) revert ZeroAddress();
        address current = vendorWallet[vendorId];
        if (current == address(0)) revert VendorNotConfigured();
        if (current != msg.sender) revert NotVendorWallet();
        if (newWallet == current) revert SameWallet();
        vendorWallet[vendorId] = newWallet;
        emit VendorWalletChanged(vendorId, newWallet);
    }

    /// @notice Withdraw your whole `payableOf` to yourself: seller earnings and relay fees alike. Never paused.
    function withdrawPayable() external nonReentrant returns (uint256) {
        return _withdrawPayable();
    }

    /// @notice Original name of `withdrawPayable`; identical behaviour, kept for existing seller tooling.
    function withdrawVendor() external nonReentrant {
        _withdrawPayable();
    }

    function _withdrawPayable() private returns (uint256 amount) {
        amount = payableOf[msg.sender];
        if (amount == 0) revert InsufficientPayable();
        payableOf[msg.sender] = 0;
        totalLiabilities -= amount;
        _transfer(msg.sender, amount);
        emit VendorWithdrawn(msg.sender, amount);
    }

    /// @notice Send all accrued fees to the current feeRecipient. Anyone may trigger it. Never paused.
    function withdrawFees() external nonReentrant {
        uint256 amount = accruedFees;
        if (amount == 0) revert ZeroAmount();
        accruedFees = 0;
        totalLiabilities -= amount;
        address to = feeRecipient;
        _transfer(to, amount);
        emit FeesWithdrawn(to, amount);
    }

    // ── owner (complete list) ─────────────────────────────────────────────────

    /// @notice Change where future fee withdrawals go (protocol fees only; never buyer or seller funds).
    function setFeeRecipient(address next) external onlyOwner {
        if (next == address(0)) revert ZeroAddress();
        if (next == address(this)) revert FeeRecipientIsSelf();
        emit FeeRecipientUpdated(feeRecipient, next);
        feeRecipient = next;
    }

    /// @notice Emergency lever: blocks `fund` and `startLease` only. Cancel, finalize, claimEarned, cashOut,
    ///         withdrawVendor and withdrawFees keep working, so a pause can never trap money.
    function setPaused(bool next) external onlyOwner {
        paused = next;
        emit PauseSet(next);
    }

    /// @notice Step 1 of handover. `address(0)` cancels a pending handover.
    function transferOwnership(address newOwner) external onlyOwner {
        pendingOwner = newOwner;
        emit OwnershipTransferStarted(owner, newOwner);
    }

    /// @notice Step 2 of handover, called by the pending owner.
    function acceptOwnership() external {
        if (msg.sender != pendingOwner) revert NotPendingOwner();
        emit OwnershipTransferred(owner, msg.sender);
        owner = msg.sender;
        pendingOwner = address(0);
    }

    /// @notice USDC held above `totalLiabilities` (direct transfers). Owed to nobody.
    function surplusUsdc() public view returns (uint256) {
        uint256 bal = usdc.balanceOf(address(this));
        return bal > totalLiabilities ? bal - totalLiabilities : 0;
    }

    /// @notice Send the USDC surplus to the fee recipient. Bounded by `surplusUsdc()`, so it can never reach
    ///         an amount owed to a buyer, seller, relayer, held claimant, fees or `unattributedUsdc`.
    function sweepSurplus() external nonReentrant onlyOwner returns (uint256 amount) {
        amount = surplusUsdc();
        if (amount == 0) revert ZeroAmount();
        address to = feeRecipient;
        _transfer(to, amount);
        if (usdc.balanceOf(address(this)) < totalLiabilities) revert ExceedsUntracked();
        emit SurplusSwept(to, amount);
    }

    /// @notice Recover tokens sent here by mistake. Never USDC (use `sweepSurplus` for USDC surplus).
    ///         Never tokens held for a user (`heldTotal`).
    function rescueToken(address tokenAddr, address to, uint256 amount) external nonReentrant onlyOwner {
        if (tokenAddr == address(usdc)) revert CannotRescueUsdc();
        if (to == address(0)) revert ZeroAddress();
        if (amount == 0) revert ZeroAmount();
        if (tokenAddr == NATIVE) {
            if (amount > address(this).balance - heldTotal[NATIVE]) revert ExceedsUntracked();
            (bool sent,) = to.call{value: amount}("");
            if (!sent) revert NativeSendFailed();
        } else {
            if (amount > IERC20(tokenAddr).balanceOf(address(this)) - heldTotal[tokenAddr]) {
                revert ExceedsUntracked();
            }
            _call(tokenAddr, abi.encodeCall(IERC20.transfer, (to, amount)));
        }
        emit TokenRescued(tokenAddr, to, amount);
    }

    // ── views ─────────────────────────────────────────────────────────────────

    /// @notice Hourly rate and whole hours of a lease.
    function leaseTerms(uint256 leaseId) external view returns (uint256 ratePerHour, uint256 hoursBought) {
        Lease storage l = _leases[leaseId];
        if (l.duration == 0) return (0, 0);
        hoursBought = uint256(l.duration) / HOUR;
        ratePerHour = uint256(l.amount) / hoursBought;
    }

    /// @notice Full lease record.
    function leaseInfo(uint256 leaseId)
        external
        view
        returns (
            address buyer,
            address sellerWallet,
            uint256 amount,
            uint256 start,
            uint256 duration,
            uint256 credited,
            uint256 endedAt,
            bool closed
        )
    {
        Lease storage l = _leases[leaseId];
        return (l.buyer, l.sellerWallet, l.amount, l.start, l.duration, l.credited, l.endedAt, l.closed);
    }

    /// @notice Lease extras: vendor id, whether the seller stopped it, and the end of paid service time.
    function leaseExtra(uint256 leaseId)
        external
        view
        returns (bytes32 vendorId, bool sellerStopped, uint256 paidUntil)
    {
        Lease storage l = _leases[leaseId];
        uint256 end = l.buyer == address(0) ? 0 : uint256(l.start) + l.duration;
        return (l.vendorId, l.sellerStopped, l.closed ? l.endedAt : end);
    }

    /// @notice Hold-style view used by the seller watcher. `deadline` = scheduled end (or the end of paid time once
    ///         closed). `active` = open and before the scheduled end (a renewal starting later is already
    ///         active, so the watcher can admit it without a gap).
    function holdInfo(uint256 leaseId)
        external
        view
        returns (address user, uint256 amount, address committedWallet, uint256 deadline, bool active)
    {
        Lease storage l = _leases[leaseId];
        uint256 end = l.buyer == address(0) ? 0 : uint256(l.start) + l.duration;
        deadline = l.closed ? l.endedAt : end;
        active = !l.closed && block.timestamp < end;
        return (l.buyer, l.amount, l.sellerWallet, deadline, active);
    }

    // ── internal ──────────────────────────────────────────────────────────────

    /// @dev rate * hours begun. 0 before start; the current hour counts as soon as it begins.
    function _earned(Lease storage l) private view returns (uint256) {
        if (block.timestamp < l.start) return 0;
        uint256 hrs = uint256(l.duration) / HOUR;
        uint256 begun = (block.timestamp - l.start) / HOUR + 1;
        if (begun >= hrs) return l.amount;
        return (uint256(l.amount) / hrs) * begun;
    }

    function _creditSeller(uint256 leaseId, Lease storage l, uint256 amount) private {
        address wallet = l.sellerWallet;
        l.credited += uint96(amount);
        lockedOf[l.buyer] -= amount;
        payableOf[wallet] += amount;
        emit PayableCredited(wallet, amount, leaseId);
    }

    function _close(uint256 leaseId, Lease storage l, address by) private {
        uint256 total = _earned(l);
        uint256 toSeller = total - l.credited;
        uint256 toBuyer = uint256(l.amount) - total;
        l.closed = true;
        uint256 end = uint256(l.start) + l.duration;
        if (by != address(0) && block.timestamp < end) {
            l.sellerStopped = true;
            uint256 hrs = uint256(l.duration) / HOUR;
            uint256 paidHours = hrs == 0 ? 0 : total / (uint256(l.amount) / hrs);
            l.endedAt = uint64(uint256(l.start) + paidHours * HOUR);
        } else {
            l.endedAt = uint64(end);
        }
        if (toSeller != 0) _creditSeller(leaseId, l, toSeller);
        if (toBuyer != 0) {
            lockedOf[l.buyer] -= toBuyer;
            availableOf[l.buyer] += toBuyer;
            emit HoldReturned(leaseId, l.buyer, toBuyer);
        }
        emit LeaseClosed(leaseId, by, toSeller, toBuyer);
    }

    /// @dev A 20-byte srcAddress (EVM-style sender of a Vault swap) or address(0).
    function _evmSrc(bytes calldata src) private view returns (address a) {
        if (src.length != 20) return address(0);
        a = address(bytes20(src));
        if (a == address(this) || a == cfVault || a == address(usdc)) return address(0);
    }

    function _decodeCcm(bytes calldata m) private view returns (bool ok, address who, uint256 walletFloat) {
        if (m.length != 21 && m.length != 29) return (false, address(0), 0);
        if (uint8(m[0]) != CCM_MSG_VERSION) return (false, address(0), 0);
        who = address(bytes20(m[1:21]));
        if (who == address(0) || who == address(this) || who == cfVault || who == address(usdc)) {
            return (false, address(0), 0);
        }
        if (m.length == 29) {
            walletFloat = uint256(uint64(bytes8(m[21:29])));
            if (walletFloat > MAX_WALLET_FLOAT) walletFloat = MAX_WALLET_FLOAT;
        }
        ok = true;
    }

    function _creditCcm(address beneficiary, uint32 srcChain, uint256 amount, uint256 walletFloat) private {
        if (walletFloat > amount) walletFloat = amount;
        if (walletFloat != 0) {
            if (_tryTransfer(address(usdc), beneficiary, walletFloat)) {
                emit CcmForwarded(beneficiary, address(usdc), walletFloat);
            } else {
                walletFloat = 0; // credit it instead
            }
        }
        uint256 toCredit = amount - walletFloat;
        uint256 fee = (toCredit * feeBps) / BPS_DENOMINATOR;
        uint256 credited = toCredit - fee;
        accruedFees += fee;
        availableOf[beneficiary] += credited;
        totalLiabilities += toCredit;
        emit CcmCredited(beneficiary, srcChain, amount, fee, credited, walletFloat);
    }

    /// @dev Best-effort push to `to`; on any failure the amount is held for `to`. Never reverts.
    function _forwardOrHold(address tokenAddr, address to, uint256 amount) private {
        bool sent;
        if (tokenAddr == NATIVE) {
            (sent,) = to.call{value: amount, gas: PUSH_GAS}("");
        } else {
            sent = _tryTransfer(tokenAddr, to, amount);
        }
        if (sent) {
            emit CcmForwarded(to, tokenAddr, amount);
        } else {
            heldOf[tokenAddr][to] += amount;
            heldTotal[tokenAddr] += amount;
            if (tokenAddr == address(usdc)) totalLiabilities += amount;
            emit CcmHeld(to, tokenAddr, amount);
        }
    }

    /// @dev ERC-20 transfer that never reverts and never copies more than 32 bytes of return data.
    function _tryTransfer(address tokenAddr, address to, uint256 amount) private returns (bool ok) {
        if (tokenAddr.code.length == 0) return false;
        bytes memory data = abi.encodeCall(IERC20.transfer, (to, amount));
        uint256 g = PUSH_GAS;
        assembly {
            mstore(0x00, 0)
            let success := call(g, tokenAddr, 0, add(data, 0x20), mload(data), 0x00, 0x20)
            let rs := returndatasize()
            // ok if call succeeded and (no return data, or a 32+ byte word that is exactly 1)
            ok := and(success, or(iszero(rs), and(gt(rs, 31), eq(mload(0x00), 1))))
        }
    }

    function _useAuth(
        address signer,
        bytes32 structHash,
        uint256 relayFee,
        address relayer,
        uint256 nonce,
        uint256 deadline,
        bytes calldata sig
    ) private {
        if (block.timestamp > deadline) revert SignatureExpired();
        if (relayer != address(0) && msg.sender != relayer) revert WrongRelayer();
        if (relayFee > maxRelayFee) revert RelayFeeTooHigh();
        if (signer == address(0)) revert BadSignature();
        if (nonceUsed[signer][nonce]) revert NonceAlreadyUsed();
        if (_recover(hashTypedData(structHash), sig) != signer) revert BadSignature();
        nonceUsed[signer][nonce] = true;
    }

    function _payRelayer(address user, uint256 fee) private {
        if (fee == 0) return;
        uint256 bal = availableOf[user];
        if (bal < fee) revert InsufficientAvailable();
        availableOf[user] = bal - fee;
        payableOf[msg.sender] += fee;
        emit RelayFeePaid(user, msg.sender, fee);
    }

    function _recover(bytes32 digest, bytes calldata sig) private pure returns (address) {
        if (sig.length != 65) revert BadSignature();
        bytes32 r = bytes32(sig[0:32]);
        bytes32 sv = bytes32(sig[32:64]);
        uint8 v = uint8(sig[64]);
        if (uint256(sv) > _HALF_N) revert BadSignature();
        if (v != 27 && v != 28) revert BadSignature();
        address who = ecrecover(digest, v, r, sv);
        if (who == address(0)) revert BadSignature();
        return who;
    }

    function _buildDomainSeparator() private view returns (bytes32) {
        return keccak256(abi.encode(_DOMAIN_TYPEHASH, _NAME_HASH, _VERSION_HASH, block.chainid, address(this)));
    }

    function _transfer(address to, uint256 amount) private {
        _call(address(usdc), abi.encodeCall(IERC20.transfer, (to, amount)));
    }

    function _validateToken(address t) private view {
        if (t.code.length == 0) revert BadToken();
        try IERC20Metadata(t).decimals() returns (uint8 d) {
            if (d != TOKEN_DECIMALS) revert BadToken();
        } catch {
            revert BadToken();
        }
        try IERC20Metadata(t).symbol() returns (string memory s) {
            if (keccak256(bytes(s)) != keccak256("USDC")) revert BadToken();
        } catch {
            revert BadToken();
        }
    }

    function _call(address target, bytes memory data) private {
        if (target.code.length == 0) revert UsdcCallFailed();
        (bool ok, bytes memory ret) = target.call(data);
        if (!ok) {
            if (ret.length > 0) {
                assembly {
                    revert(add(ret, 32), mload(ret))
                }
            }
            revert UsdcCallFailed();
        }
        if (ret.length >= 32 && !abi.decode(ret, (bool))) revert UsdcReturnedFalse();
        if (ret.length > 0 && ret.length < 32) revert UsdcReturnedFalse();
    }
}
