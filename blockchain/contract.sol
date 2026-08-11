// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title BlkMacLedger
 * @notice BlkMaC 多智能体协同框架的链上存证账本。
 *
 * 设计取舍
 * --------
 * 1. **只上链摘要，不上链明文。** 每条 Agent 交互消息在链下计算 SHA-256
 *    摘要（见 crypto/hash_tool.py::message_digest），仅把 32 字节摘要写入。
 *    原因有三：链上存储按字节计费；任务内容可能含敏感信息；
 *    防篡改只需要摘要即可完成校验。
 *
 * 2. **digest 用 bytes32 而不是 string。** SHA-256 输出恰好 32 字节，
 *    存成十六进制字符串要占 64 字节还要额外的长度字段，gas 翻倍且无收益。
 *
 * 3. **digestIndex 映射保证 O(1) 校验。** verifyDigest 若靠遍历数组，
 *    记录多了会超出 gas 上限；映射查询是常数时间。
 *    索引统一存 “真实下标 + 1”，好让 0 表示“不存在”。
 */
contract BlkMacLedger {
    struct Record {
        bytes32 digest;      // 消息内容的 SHA-256 摘要
        string  taskId;      // 所属任务 / 子任务编号
        string  sender;      // 发送方 Agent 标识
        string  receiver;    // 接收方 Agent 标识
        uint256 timestamp;   // 出块时间
        address submitter;   // 提交交易的账户
    }

    Record[] private records;

    /// @dev digest => 下标 + 1；0 表示该摘要尚未上链
    mapping(bytes32 => uint256) private digestIndex;

    /// @dev 每个 taskId 关联的记录下标，便于按任务回溯完整交互链
    mapping(string => uint256[]) private taskRecords;

    event RecordStored(
        uint256 indexed index,
        bytes32 indexed digest,
        string taskId,
        string sender,
        string receiver,
        uint256 timestamp
    );

    /**
     * @notice 存证一条交互记录。
     * @param digest   消息摘要（bytes32）
     * @param taskId   任务编号
     * @param sender   发送方标识
     * @param receiver 接收方标识
     * @return index   该记录在账本中的下标
     */
    function storeRecord(
        bytes32 digest,
        string calldata taskId,
        string calldata sender,
        string calldata receiver
    ) external returns (uint256 index) {
        require(digest != bytes32(0), "BlkMac: empty digest");
        // 重复摘要直接拒绝：既省 gas，也天然防重放
        require(digestIndex[digest] == 0, "BlkMac: duplicate digest");

        index = records.length;
        records.push(
            Record({
                digest: digest,
                taskId: taskId,
                sender: sender,
                receiver: receiver,
                timestamp: block.timestamp,
                submitter: msg.sender
            })
        );

        digestIndex[digest] = index + 1; // +1，使 0 保留给“不存在”
        taskRecords[taskId].push(index);

        emit RecordStored(index, digest, taskId, sender, receiver, block.timestamp);
    }

    /// @notice 批量存证，一笔交易写多条，显著摊薄单条记录的 gas 与延迟。
    function storeBatch(
        bytes32[] calldata digests,
        string calldata taskId,
        string calldata sender,
        string calldata receiver
    ) external returns (uint256 firstIndex) {
        require(digests.length > 0, "BlkMac: empty batch");
        firstIndex = records.length;

        for (uint256 i = 0; i < digests.length; i++) {
            bytes32 d = digests[i];
            require(d != bytes32(0), "BlkMac: empty digest");
            require(digestIndex[d] == 0, "BlkMac: duplicate digest");

            uint256 index = records.length;
            records.push(
                Record({
                    digest: d,
                    taskId: taskId,
                    sender: sender,
                    receiver: receiver,
                    timestamp: block.timestamp,
                    submitter: msg.sender
                })
            );
            digestIndex[d] = index + 1;
            taskRecords[taskId].push(index);

            emit RecordStored(index, d, taskId, sender, receiver, block.timestamp);
        }
    }

    /// @notice 防篡改校验入口：链下重算摘要后调用，返回该摘要是否已存证。
    function verifyDigest(bytes32 digest) external view returns (bool) {
        return digestIndex[digest] != 0;
    }

    /// @notice 读取指定下标的记录。
    function getRecord(uint256 index)
        external
        view
        returns (
            bytes32 digest,
            string memory taskId,
            string memory sender,
            string memory receiver,
            uint256 timestamp,
            address submitter
        )
    {
        require(index < records.length, "BlkMac: index out of range");
        Record storage r = records[index];
        return (r.digest, r.taskId, r.sender, r.receiver, r.timestamp, r.submitter);
    }

    /// @notice 按摘要反查记录下标（不存在时 revert）。
    function getIndexByDigest(bytes32 digest) external view returns (uint256) {
        uint256 stored = digestIndex[digest];
        require(stored != 0, "BlkMac: digest not found");
        return stored - 1;
    }

    /// @notice 取某个任务的全部记录下标，用于回溯完整交互链。
    function getTaskRecords(string calldata taskId) external view returns (uint256[] memory) {
        return taskRecords[taskId];
    }

    /// @notice 账本中的记录总数。
    function recordCount() external view returns (uint256) {
        return records.length;
    }
}
