// SPDX-License-Identifier: CERN-OHL-P-2.0
/*
 * The board top level: 25 MHz oscillator and one RGMII port in, one
 * ChaCha20-Poly1305 core reached over UDP out.
 *
 * The chain, and which clock each link runs on:
 *
 *   pads -> oca_rgmii              rgmii_rx_clk in, clk_tx out
 *        -> oca_eth_mac_1g_fifo_64 crosses to clk_sys itself, both ways
 *        -> oca_eth_axis_rx_64     clk_sys, strips the Ethernet header
 *        -> oca_udp_complete_64    clk_sys, ARP + IP + UDP
 *        -> oca_udp_seam           clk_sys, holds the requester's address
 *        -> oca_core               clk_sys, the crypto
 *
 * and back out the same way through oca_eth_axis_tx_64.
 *
 * RESET POLARITY. Ours is active low and asynchronous; the whole vendor
 * subtree is active high and asynchronously asserted. oca_clkrst emits
 * both forms per domain and every instance below takes the one it wants,
 * in its own domain. Getting this wrong gives a design that holds itself
 * in reset and presents at the bench as a dead PHY.
 *
 * ONE ADDRESS. The UDP stack's local_ip comes from oca_udp_seam's
 * stack_local_ip output, never from a constant here. arp.v:305 tests
 * local_ip to decide which ARP requests the board answers, and arp.v:197
 * sends it as the sender protocol address, so a second constant would
 * give a board that answers ARP for one address and replies from
 * another: requests arrive, every reply is discarded by the peer's UDP
 * layer for matching no socket, and nothing on the board notices. There
 * is a test in test_udp_seam.py pinning what the seam publishes; this
 * wire is the other half of it.
 *
 * NO MDIO. Nothing here drives the management bus. The B50612D comes up
 * with its strapped defaults, which on this board is what the vendor's
 * own designs rely on, and in-band status on the RGMII data lines is
 * enough to read link state (oca_rgmii decodes it). What that leaves
 * unconfigured, deliberately: PHY-side RGMII delays, LED behaviour,
 * auto-negotiation advertisement and the PHY's own address. If the delay
 * sweep at the bench cannot close the receive path, MDIO is the next
 * lever and it does not exist yet.
 *
 * NO RESET PIN. The .lpf constrains 17 pads and none is a reset, so
 * power-on is the only reset root and it is built here.
 */
`default_nettype none

module oca_top #(
    // Locally-administered MAC: bit 1 of the first octet set, bit 0
    // clear. Arbitrary because it has to be -- this board has no
    // configuration storage and no OUI belongs to this project -- and
    // locally-administered is the kind of arbitrary that cannot collide
    // with a real vendor's assignment.
    parameter logic [47:0] LOCAL_MAC   = 48'h02_00_5E_00_00_01,
    parameter logic [31:0] LOCAL_IP    = 32'hC0A8_0164,   // 192.168.1.100
    parameter logic [31:0] GATEWAY_IP  = 32'hC0A8_0101,   // 192.168.1.1
    parameter logic [31:0] SUBNET_MASK = 32'hFFFF_FF00,   // 255.255.255.0
    parameter logic [15:0] LOCAL_PORT  = 16'd5000
) (
    input  var  logic       clk25,

    output var  logic       led_n,

    output var  logic       phy_rst_n,
    output var  logic       phy_mdio,
    output var  logic       phy_mdc,

    input  var  logic       rgmii_rx_clk,
    input  var  logic [3:0] rgmii_rxd,
    input  var  logic       rgmii_rx_ctl,
    output var  logic       rgmii_tx_clk,
    output var  logic [3:0] rgmii_txd,
    output var  logic       rgmii_tx_ctl
);

    // ------------------------------------------------------------------
    // Power-on reset
    // ------------------------------------------------------------------
    // ECP5 flip-flops come out of configuration cleared, so this counter
    // starts at zero on its own and holds por_n low for 16 cycles of the
    // 25 MHz input. No initialiser is written: the reset value is the
    // device's, and stating it here as well would be a second source.
    logic [3:0] por_cnt;
    logic       por_n;

    always_ff @(posedge clk25) begin
        if (!por_n) begin
            por_cnt <= por_cnt + 4'd1;
        end
    end

    always_comb por_n = (por_cnt == 4'd15);

    // ------------------------------------------------------------------
    // Clocks and resets
    // ------------------------------------------------------------------
    logic clk_sys, clk_tx, pll_locked;
    logic rst_n_sys, rst_n_tx, rst_n_rx;
    logic rst_sys, rst_tx, rst_rx;
    logic phy_ready;

    logic       gmii_rx_clk;
    logic [7:0] gmii_rxd;
    logic       gmii_rx_dv, gmii_rx_er;
    logic [7:0] gmii_txd;
    logic       gmii_tx_en, gmii_tx_er;
    logic       link_up, link_full_duplex;
    logic [1:0] link_speed;
    logic       dly_cflag;

    oca_clkrst u_clkrst (
        .clk_in     (clk25),
        .ext_rst_n  (por_n),
        .clk_rx     (gmii_rx_clk),
        .clk_sys    (clk_sys),
        .clk_tx     (clk_tx),
        .pll_locked (pll_locked),
        .rst_n_sys  (rst_n_sys),
        .rst_n_tx   (rst_n_tx),
        .rst_n_rx   (rst_n_rx),
        .rst_sys    (rst_sys),
        .rst_tx     (rst_tx),
        .rst_rx     (rst_rx),
        .phy_rst_n  (phy_rst_n),
        .phy_ready  (phy_ready)
    );

    // ------------------------------------------------------------------
    // RGMII front end
    // ------------------------------------------------------------------
    // rst_n_rx: in the synthesised branch the only thing this reset
    // reaches inside oca_rgmii is the in-band status register, clocked by
    // rgmii_rx_clk.
    oca_rgmii #(
        .SIMULATION (1'b0)
    ) u_rgmii (
        .rgmii_rx_clk     (rgmii_rx_clk),
        .rgmii_rxd        (rgmii_rxd),
        .rgmii_rx_ctl     (rgmii_rx_ctl),
        .rgmii_tx_clk     (rgmii_tx_clk),
        .rgmii_txd        (rgmii_txd),
        .rgmii_tx_ctl     (rgmii_tx_ctl),
        .gmii_rx_clk      (gmii_rx_clk),
        .gmii_rxd         (gmii_rxd),
        .gmii_rx_dv       (gmii_rx_dv),
        .gmii_rx_er       (gmii_rx_er),
        .gmii_tx_clk      (clk_tx),
        .gmii_txd         (gmii_txd),
        .gmii_tx_en       (gmii_tx_en),
        .gmii_tx_er       (gmii_tx_er),
        .dly_loadn        (1'b1),
        .dly_move         (1'b0),
        .dly_direction    (1'b0),
        .dly_cflag        (dly_cflag),
        .rst_n            (rst_n_rx),
        .link_up          (link_up),
        .link_speed       (link_speed),
        .link_full_duplex (link_full_duplex)
    );

    // ------------------------------------------------------------------
    // MAC
    // ------------------------------------------------------------------
    logic [63:0] mac_rx_tdata, mac_tx_tdata;
    logic [7:0]  mac_rx_tkeep, mac_tx_tkeep;
    logic        mac_rx_tvalid, mac_rx_tready, mac_rx_tlast, mac_rx_tuser;
    logic        mac_tx_tvalid, mac_tx_tready, mac_tx_tlast, mac_tx_tuser;

    logic tx_error_underflow, tx_fifo_overflow, tx_fifo_bad_frame;
    logic tx_fifo_good_frame, rx_error_bad_frame, rx_error_bad_fcs;
    logic rx_fifo_overflow, rx_fifo_bad_frame, rx_fifo_good_frame;

    oca_eth_mac_1g_fifo_64 u_mac (
        .rx_clk         (gmii_rx_clk),
        .rx_rst         (rst_rx),
        .tx_clk         (clk_tx),
        .tx_rst         (rst_tx),
        .logic_clk      (clk_sys),
        .logic_rst      (rst_sys),

        .tx_axis_tdata  (mac_tx_tdata),
        .tx_axis_tkeep  (mac_tx_tkeep),
        .tx_axis_tvalid (mac_tx_tvalid),
        .tx_axis_tready (mac_tx_tready),
        .tx_axis_tlast  (mac_tx_tlast),
        .tx_axis_tuser  (mac_tx_tuser),

        .rx_axis_tdata  (mac_rx_tdata),
        .rx_axis_tkeep  (mac_rx_tkeep),
        .rx_axis_tvalid (mac_rx_tvalid),
        .rx_axis_tready (mac_rx_tready),
        .rx_axis_tlast  (mac_rx_tlast),
        .rx_axis_tuser  (mac_rx_tuser),

        .gmii_rxd       (gmii_rxd),
        .gmii_rx_dv     (gmii_rx_dv),
        .gmii_rx_er     (gmii_rx_er),
        .gmii_txd       (gmii_txd),
        .gmii_tx_en     (gmii_tx_en),
        .gmii_tx_er     (gmii_tx_er),

        // The RGMII front end delivers one byte per clock at 1 Gbps and
        // this design does not run at 10 or 100 Mbps: both enables are
        // held on and neither MII select is taken.
        .rx_clk_enable  (1'b1),
        .tx_clk_enable  (1'b1),
        .rx_mii_select  (1'b0),
        .tx_mii_select  (1'b0),

        .tx_error_underflow (tx_error_underflow),
        .tx_fifo_overflow   (tx_fifo_overflow),
        .tx_fifo_bad_frame  (tx_fifo_bad_frame),
        .tx_fifo_good_frame (tx_fifo_good_frame),
        .rx_error_bad_frame (rx_error_bad_frame),
        .rx_error_bad_fcs   (rx_error_bad_fcs),
        .rx_fifo_overflow   (rx_fifo_overflow),
        .rx_fifo_bad_frame  (rx_fifo_bad_frame),
        .rx_fifo_good_frame (rx_fifo_good_frame),

        // 12 octets is the standard inter-frame gap.
        .cfg_ifg        (8'd12),
        .cfg_tx_enable  (1'b1),
        .cfg_rx_enable  (1'b1)
    );

    // ------------------------------------------------------------------
    // Ethernet header parse and build
    // ------------------------------------------------------------------
    logic        rx_eth_hdr_valid, rx_eth_hdr_ready;
    logic [47:0] rx_eth_dest_mac, rx_eth_src_mac;
    logic [15:0] rx_eth_type;
    logic [63:0] rx_eth_payload_tdata;
    logic [7:0]  rx_eth_payload_tkeep;
    logic        rx_eth_payload_tvalid, rx_eth_payload_tready;
    logic        rx_eth_payload_tlast, rx_eth_payload_tuser;
    logic        eth_rx_busy, eth_rx_early_term;

    oca_eth_axis_rx_64 u_eth_rx (
        .clk                            (clk_sys),
        .rst                            (rst_sys),
        .s_axis_tdata                   (mac_rx_tdata),
        .s_axis_tkeep                   (mac_rx_tkeep),
        .s_axis_tvalid                  (mac_rx_tvalid),
        .s_axis_tready                  (mac_rx_tready),
        .s_axis_tlast                   (mac_rx_tlast),
        .s_axis_tuser                   (mac_rx_tuser),
        .m_eth_hdr_valid                (rx_eth_hdr_valid),
        .m_eth_hdr_ready                (rx_eth_hdr_ready),
        .m_eth_dest_mac                 (rx_eth_dest_mac),
        .m_eth_src_mac                  (rx_eth_src_mac),
        .m_eth_type                     (rx_eth_type),
        .m_eth_payload_axis_tdata       (rx_eth_payload_tdata),
        .m_eth_payload_axis_tkeep       (rx_eth_payload_tkeep),
        .m_eth_payload_axis_tvalid      (rx_eth_payload_tvalid),
        .m_eth_payload_axis_tready      (rx_eth_payload_tready),
        .m_eth_payload_axis_tlast       (rx_eth_payload_tlast),
        .m_eth_payload_axis_tuser       (rx_eth_payload_tuser),
        .busy                           (eth_rx_busy),
        .error_header_early_termination (eth_rx_early_term)
    );

    logic        tx_eth_hdr_valid, tx_eth_hdr_ready;
    logic [47:0] tx_eth_dest_mac, tx_eth_src_mac;
    logic [15:0] tx_eth_type;
    logic [63:0] tx_eth_payload_tdata;
    logic [7:0]  tx_eth_payload_tkeep;
    logic        tx_eth_payload_tvalid, tx_eth_payload_tready;
    logic        tx_eth_payload_tlast, tx_eth_payload_tuser;
    logic        eth_tx_busy;

    oca_eth_axis_tx_64 u_eth_tx (
        .clk                       (clk_sys),
        .rst                       (rst_sys),
        .s_eth_hdr_valid           (tx_eth_hdr_valid),
        .s_eth_hdr_ready           (tx_eth_hdr_ready),
        .s_eth_dest_mac            (tx_eth_dest_mac),
        .s_eth_src_mac             (tx_eth_src_mac),
        .s_eth_type                (tx_eth_type),
        .s_eth_payload_axis_tdata  (tx_eth_payload_tdata),
        .s_eth_payload_axis_tkeep  (tx_eth_payload_tkeep),
        .s_eth_payload_axis_tvalid (tx_eth_payload_tvalid),
        .s_eth_payload_axis_tready (tx_eth_payload_tready),
        .s_eth_payload_axis_tlast  (tx_eth_payload_tlast),
        .s_eth_payload_axis_tuser  (tx_eth_payload_tuser),
        .m_axis_tdata              (mac_tx_tdata),
        .m_axis_tkeep              (mac_tx_tkeep),
        .m_axis_tvalid             (mac_tx_tvalid),
        .m_axis_tready             (mac_tx_tready),
        .m_axis_tlast              (mac_tx_tlast),
        .m_axis_tuser              (mac_tx_tuser),
        .busy                      (eth_tx_busy)
    );

    // ------------------------------------------------------------------
    // ARP, IP and UDP
    // ------------------------------------------------------------------
    logic        udp_rx_hdr_valid, udp_rx_hdr_ready;
    logic [31:0] udp_rx_source_ip;
    logic [15:0] udp_rx_source_port, udp_rx_dest_port;
    logic [63:0] udp_rx_payload_tdata;
    logic [7:0]  udp_rx_payload_tkeep;
    logic        udp_rx_payload_tvalid, udp_rx_payload_tready;
    logic        udp_rx_payload_tlast, udp_rx_payload_tuser;

    logic        udp_tx_hdr_valid, udp_tx_hdr_ready;
    logic [5:0]  udp_tx_ip_dscp;
    logic [1:0]  udp_tx_ip_ecn;
    logic [7:0]  udp_tx_ip_ttl;
    logic [31:0] udp_tx_ip_source_ip, udp_tx_ip_dest_ip;
    logic [15:0] udp_tx_source_port, udp_tx_dest_port;
    logic [15:0] udp_tx_length, udp_tx_checksum;
    logic [63:0] udp_tx_payload_tdata;
    logic [7:0]  udp_tx_payload_tkeep;
    logic        udp_tx_payload_tvalid, udp_tx_payload_tready;
    logic        udp_tx_payload_tlast, udp_tx_payload_tuser;

    logic [31:0] stack_local_ip;

    logic ip_rx_busy, ip_tx_busy, udp_rx_busy, udp_tx_busy;
    logic ip_rx_err_hdr_early, ip_rx_err_payload_early, ip_rx_err_invalid_hdr;
    logic ip_rx_err_invalid_csum, ip_tx_err_payload_early, ip_tx_err_arp_failed;
    logic udp_rx_err_hdr_early, udp_rx_err_payload_early;
    logic udp_tx_err_payload_early;

    oca_udp_complete_64 u_udp (
        .clk (clk_sys),
        .rst (rst_sys),

        .s_eth_hdr_valid           (rx_eth_hdr_valid),
        .s_eth_hdr_ready           (rx_eth_hdr_ready),
        .s_eth_dest_mac            (rx_eth_dest_mac),
        .s_eth_src_mac             (rx_eth_src_mac),
        .s_eth_type                (rx_eth_type),
        .s_eth_payload_axis_tdata  (rx_eth_payload_tdata),
        .s_eth_payload_axis_tkeep  (rx_eth_payload_tkeep),
        .s_eth_payload_axis_tvalid (rx_eth_payload_tvalid),
        .s_eth_payload_axis_tready (rx_eth_payload_tready),
        .s_eth_payload_axis_tlast  (rx_eth_payload_tlast),
        .s_eth_payload_axis_tuser  (rx_eth_payload_tuser),

        .m_eth_hdr_valid           (tx_eth_hdr_valid),
        .m_eth_hdr_ready           (tx_eth_hdr_ready),
        .m_eth_dest_mac            (tx_eth_dest_mac),
        .m_eth_src_mac             (tx_eth_src_mac),
        .m_eth_type                (tx_eth_type),
        .m_eth_payload_axis_tdata  (tx_eth_payload_tdata),
        .m_eth_payload_axis_tkeep  (tx_eth_payload_tkeep),
        .m_eth_payload_axis_tvalid (tx_eth_payload_tvalid),
        .m_eth_payload_axis_tready (tx_eth_payload_tready),
        .m_eth_payload_axis_tlast  (tx_eth_payload_tlast),
        .m_eth_payload_axis_tuser  (tx_eth_payload_tuser),

        // The raw IP receive path, which every IPv4 frame whose protocol
        // is not 0x11 leaves by -- an ICMP echo request, a stray TCP
        // segment, anything. udp_complete_64.v:361-365 builds
        // ip_rx_ip_hdr_ready from these two, so with both reading 0 the
        // first such frame is never consumed, ip_eth_rx_64 holds it
        // forever and the whole receive path stops behind it with no
        // error wire raised. Nothing here wants the frame, so it is
        // accepted and discarded, which is what oca_udp_complete_64.v:29-44
        // prescribes.
        .m_ip_hdr_ready            (1'b1),
        .m_ip_payload_axis_tready  (1'b1),

        .s_udp_hdr_valid           (udp_tx_hdr_valid),
        .s_udp_hdr_ready           (udp_tx_hdr_ready),
        .s_udp_ip_dscp             (udp_tx_ip_dscp),
        .s_udp_ip_ecn              (udp_tx_ip_ecn),
        .s_udp_ip_ttl              (udp_tx_ip_ttl),
        .s_udp_ip_source_ip        (udp_tx_ip_source_ip),
        .s_udp_ip_dest_ip          (udp_tx_ip_dest_ip),
        .s_udp_source_port         (udp_tx_source_port),
        .s_udp_dest_port           (udp_tx_dest_port),
        .s_udp_length              (udp_tx_length),
        .s_udp_checksum            (udp_tx_checksum),
        .s_udp_payload_axis_tdata  (udp_tx_payload_tdata),
        .s_udp_payload_axis_tkeep  (udp_tx_payload_tkeep),
        .s_udp_payload_axis_tvalid (udp_tx_payload_tvalid),
        .s_udp_payload_axis_tready (udp_tx_payload_tready),
        .s_udp_payload_axis_tlast  (udp_tx_payload_tlast),
        .s_udp_payload_axis_tuser  (udp_tx_payload_tuser),

        .m_udp_hdr_valid           (udp_rx_hdr_valid),
        .m_udp_hdr_ready           (udp_rx_hdr_ready),

        // verilator lint_off PINCONNECTEMPTY
        //
        // The stack presents all 22 header fields; the seam needs three
        // of them -- the peer's IP, its port, and ours -- to build a
        // reply. The rest are left open deliberately rather than wired to
        // signals nobody reads, which would trade this warning for an
        // UNUSED one and add nets to the netlist for no reason. Anything
        // a later design wants back is one line away.
        .m_udp_eth_dest_mac        (),
        .m_udp_eth_src_mac         (),
        .m_udp_eth_type            (),
        .m_udp_ip_version          (),
        .m_udp_ip_ihl              (),
        .m_udp_ip_dscp             (),
        .m_udp_ip_ecn              (),
        .m_udp_ip_length           (),
        .m_udp_ip_identification   (),
        .m_udp_ip_flags            (),
        .m_udp_ip_fragment_offset  (),
        .m_udp_ip_ttl              (),
        .m_udp_ip_protocol         (),
        .m_udp_ip_header_checksum  (),
        .m_udp_ip_source_ip        (udp_rx_source_ip),
        .m_udp_ip_dest_ip          (),
        .m_udp_source_port         (udp_rx_source_port),
        .m_udp_dest_port           (udp_rx_dest_port),
        .m_udp_length              (),
        .m_udp_checksum            (),
        // verilator lint_on PINCONNECTEMPTY
        .m_udp_payload_axis_tdata  (udp_rx_payload_tdata),
        .m_udp_payload_axis_tkeep  (udp_rx_payload_tkeep),
        .m_udp_payload_axis_tvalid (udp_rx_payload_tvalid),
        .m_udp_payload_axis_tready (udp_rx_payload_tready),
        .m_udp_payload_axis_tlast  (udp_rx_payload_tlast),
        .m_udp_payload_axis_tuser  (udp_rx_payload_tuser),

        .ip_rx_busy                        (ip_rx_busy),
        .ip_tx_busy                        (ip_tx_busy),
        .udp_rx_busy                       (udp_rx_busy),
        .udp_tx_busy                       (udp_tx_busy),
        .ip_rx_error_header_early_termination  (ip_rx_err_hdr_early),
        .ip_rx_error_payload_early_termination (ip_rx_err_payload_early),
        .ip_rx_error_invalid_header            (ip_rx_err_invalid_hdr),
        .ip_rx_error_invalid_checksum          (ip_rx_err_invalid_csum),
        .ip_tx_error_payload_early_termination (ip_tx_err_payload_early),
        .ip_tx_error_arp_failed                (ip_tx_err_arp_failed),
        .udp_rx_error_header_early_termination  (udp_rx_err_hdr_early),
        .udp_rx_error_payload_early_termination (udp_rx_err_payload_early),
        // There is no udp_tx_error_header_early_termination: the
        // transmit side cannot terminate early inside a header it builds
        // itself, so upstream does not expose one.
        .udp_tx_error_payload_early_termination (udp_tx_err_payload_early),

        .local_mac   (LOCAL_MAC),
        .local_ip    (stack_local_ip),
        .gateway_ip  (GATEWAY_IP),
        .subnet_mask (SUBNET_MASK),

        // Nothing flushes the cache at runtime. Stated rather than left to
        // the toolchain: it gates arp_cache's query and write ports
        // (arp_cache.v:158, :181), and a pin that reads 0 because two
        // flows happen to resolve undriven nets that way is not the same
        // thing as a pin that is held low on purpose.
        .clear_arp_cache (1'b0)
    );

    // ------------------------------------------------------------------
    // The seam, and the core
    // ------------------------------------------------------------------
    logic [63:0] core_s_tdata, core_m_tdata;
    logic [7:0]  core_s_tkeep, core_m_tkeep;
    logic        core_s_tvalid, core_s_tready, core_s_tlast;
    logic        core_m_tvalid, core_m_tready, core_m_tlast;

    logic [31:0] cnt_accepted, cnt_drop_short, cnt_drop_port, cnt_drop_full;
    logic [31:0] cnt_drop_nohdr, cnt_tuser, cnt_resp_orphan;
    logic [3:0]  hdr_q_watermark;

    oca_udp_seam #(
        .LOCAL_IP   (LOCAL_IP),
        .LOCAL_PORT (LOCAL_PORT)
    ) u_seam (
        .clk               (clk_sys),
        .rst_n             (rst_n_sys),
        .stack_local_ip    (stack_local_ip),

        .rx_hdr_valid      (udp_rx_hdr_valid),
        .rx_hdr_ready      (udp_rx_hdr_ready),
        .rx_ip_source_ip   (udp_rx_source_ip),
        .rx_source_port    (udp_rx_source_port),
        .rx_dest_port      (udp_rx_dest_port),
        .rx_payload_tdata  (udp_rx_payload_tdata),
        .rx_payload_tkeep  (udp_rx_payload_tkeep),
        .rx_payload_tvalid (udp_rx_payload_tvalid),
        .rx_payload_tready (udp_rx_payload_tready),
        .rx_payload_tlast  (udp_rx_payload_tlast),
        .rx_payload_tuser  (udp_rx_payload_tuser),

        .tx_hdr_valid      (udp_tx_hdr_valid),
        .tx_hdr_ready      (udp_tx_hdr_ready),
        .tx_ip_dscp        (udp_tx_ip_dscp),
        .tx_ip_ecn         (udp_tx_ip_ecn),
        .tx_ip_ttl         (udp_tx_ip_ttl),
        .tx_ip_source_ip   (udp_tx_ip_source_ip),
        .tx_ip_dest_ip     (udp_tx_ip_dest_ip),
        .tx_source_port    (udp_tx_source_port),
        .tx_dest_port      (udp_tx_dest_port),
        .tx_length         (udp_tx_length),
        .tx_checksum       (udp_tx_checksum),
        .tx_payload_tdata  (udp_tx_payload_tdata),
        .tx_payload_tkeep  (udp_tx_payload_tkeep),
        .tx_payload_tvalid (udp_tx_payload_tvalid),
        .tx_payload_tready (udp_tx_payload_tready),
        .tx_payload_tlast  (udp_tx_payload_tlast),
        .tx_payload_tuser  (udp_tx_payload_tuser),

        .core_s_tdata      (core_s_tdata),
        .core_s_tkeep      (core_s_tkeep),
        .core_s_tvalid     (core_s_tvalid),
        .core_s_tready     (core_s_tready),
        .core_s_tlast      (core_s_tlast),
        .core_m_tdata      (core_m_tdata),
        .core_m_tkeep      (core_m_tkeep),
        .core_m_tvalid     (core_m_tvalid),
        .core_m_tready     (core_m_tready),
        .core_m_tlast      (core_m_tlast),

        .cnt_accepted      (cnt_accepted),
        .cnt_drop_short    (cnt_drop_short),
        .cnt_drop_port     (cnt_drop_port),
        .cnt_drop_full     (cnt_drop_full),
        .cnt_drop_nohdr    (cnt_drop_nohdr),
        .cnt_tuser         (cnt_tuser),
        .cnt_resp_orphan   (cnt_resp_orphan),
        .hdr_q_watermark   (hdr_q_watermark)
    );

    oca_core u_core (
        .clk           (clk_sys),
        .rst_n         (rst_n_sys),
        .s_axis_tdata  (core_s_tdata),
        .s_axis_tkeep  (core_s_tkeep),
        .s_axis_tvalid (core_s_tvalid),
        .s_axis_tready (core_s_tready),
        .s_axis_tlast  (core_s_tlast),
        .m_axis_tdata  (core_m_tdata),
        .m_axis_tkeep  (core_m_tkeep),
        .m_axis_tvalid (core_m_tvalid),
        .m_axis_tready (core_m_tready),
        .m_axis_tlast  (core_m_tlast)
    );

    // ------------------------------------------------------------------
    // What the one LED says
    // ------------------------------------------------------------------
    //
    // Every status and error output above reaches this logic. That is not
    // tidiness: an output nobody reads is an output yosys deletes, and it
    // takes the logic behind it along, so a design that ignores its own
    // drop counters is a design that does not have them. The bring-up
    // sequence asks for the drop counters to be visible BEFORE traffic
    // runs, and one LED is what this board has.
    //
    //   dark            no bitstream, or the PLL never locked
    //   slow blink      clocks up, link down
    //   fast blink      link up, nothing has gone wrong yet
    //   solid on        something was dropped or errored, and it is
    //                   sticky: it stays on until the board is reset
    //
    // Sticky rather than momentary because a single dropped frame at
    // 1 Gbps is invisible to an eye, and the question at the bench is
    // "did anything go wrong at all", not "is it going wrong now".
    logic any_error;

    always_comb any_error =
        // the seam's own accounting
        (cnt_drop_short  != 32'd0) || (cnt_drop_port   != 32'd0) ||
        (cnt_drop_full   != 32'd0) || (cnt_drop_nohdr  != 32'd0) ||
        (cnt_tuser       != 32'd0) || (cnt_resp_orphan != 32'd0) ||
        // the MAC's
        tx_error_underflow || tx_fifo_overflow || tx_fifo_bad_frame ||
        rx_error_bad_frame || rx_error_bad_fcs || rx_fifo_overflow  ||
        rx_fifo_bad_frame  ||
        // the header parser's
        eth_rx_early_term ||
        // the stack's
        ip_rx_err_hdr_early  || ip_rx_err_payload_early ||
        ip_rx_err_invalid_hdr || ip_rx_err_invalid_csum ||
        ip_tx_err_payload_early || ip_tx_err_arp_failed ||
        udp_rx_err_hdr_early || udp_rx_err_payload_early ||
        udp_tx_err_payload_early;

    logic error_seen;

    always_ff @(posedge clk_sys or negedge rst_n_sys) begin
        if (!rst_n_sys) begin
            error_seen <= 1'b0;
        end else if (any_error) begin
            error_seen <= 1'b1;
        end
    end

    logic [25:0] beat;

    always_ff @(posedge clk_sys or negedge rst_n_sys) begin
        if (!rst_n_sys) begin
            beat <= 26'd0;
        end else begin
            beat <= beat + 26'd1;
        end
    end

    // The remaining signals have no place in the LED's meaning but must
    // not dangle: busy and good-frame strobes move constantly, so folding
    // them into the blink would make it unreadable. They are OR-reduced
    // into a bit that cannot change what the LED shows, which is what
    // stops `verilator --lint-only -Wall` calling them unused.
    //
    // It does not survive synthesis and is not meant to: `activity &
    // 1'b0` folds to zero, so no net of that name is in the netlist and
    // `led_n` is exactly `~lit`. This comment claimed the opposite until
    // 2026-08-11 -- that the bit "cannot be optimised away" -- which was
    // wrong in a way nothing would have caught, since the construct does
    // its real job either way.
    logic activity;

    always_comb activity = |{tx_fifo_good_frame, rx_fifo_good_frame,
                             eth_rx_busy, eth_tx_busy,
                             ip_rx_busy, ip_tx_busy,
                             udp_rx_busy, udp_tx_busy,
                             hdr_q_watermark, cnt_accepted,
                             phy_ready, dly_cflag,
                             link_full_duplex, link_speed};

    logic lit;

    always_comb begin
        if (!pll_locked)     lit = 1'b0;
        else if (error_seen) lit = 1'b1;
        else if (!link_up)   lit = beat[25];
        else                 lit = beat[23];
    end

    always_comb led_n = ~(lit | (activity & 1'b0));

    // MDIO idles: the bus is pulled high and nobody is driving the clock.
    always_comb phy_mdc  = 1'b0;
    always_comb phy_mdio = 1'b1;

endmodule

`default_nettype wire
