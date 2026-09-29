from os import environ
from dotenv import load_dotenv

from agno.agent import Agent
from agno.models.openai.like import OpenAILike

load_dotenv()

agent = Agent(
    model=OpenAILike(
        id=environ["MANTLE_MODEL_ID"],
        api_key=environ["MANTLE_API_KEY"],
        base_url=environ["MANTLE_BASE_URL"],
    ),
    markdown=True,
    instructions="""You are expert in ABAP Code and you are given with ABAP Code.
                  You are tasked to analyze it and identify the tables used to extract data for 2LIS_11_VAITM.
                  
                  ### ABAP Code:
                 FUNCTION mcex_bw_lo_api.
                    *"----------------------------------------------------------------------
                    *"*"Local Interface:
                    *"  IMPORTING
                    *"     VALUE(I_ISOURCE) TYPE  SBIWA_S_INTERFACE-ISOURCE OPTIONAL
                    *"     VALUE(I_REQUNR) TYPE  SBIWA_S_INTERFACE-REQUNR
                    *"     VALUE(I_MAXSIZE) TYPE  SBIWA_S_INTERFACE-MAXSIZE DEFAULT 1000
                    *"     VALUE(I_INITFLAG) TYPE  SBIWA_S_INTERFACE-INITFLAG OPTIONAL
                    *"     VALUE(I_UPDMODE) TYPE  SBIWA_S_INTERFACE-UPDMODE OPTIONAL
                    *"     VALUE(I_DATAPAKID) TYPE  SBIWA_S_INTERFACE-DATAPAKID DEFAULT
                    *"       50000
                    *"     VALUE(I_READ_ONLY) TYPE  SBIW_BOOL DEFAULT SBIW_C_FALSE
                    *"  TABLES
                    *"      I_T_SELECT TYPE  SBIWA_T_SELECT OPTIONAL
                    *"      I_T_FIELDS TYPE  SBIWA_T_FIELDS OPTIONAL
                    *"      E_T_DATA OPTIONAL
                    *"  EXCEPTIONS
                    *"      NO_MORE_DATA
                    *"      ERROR_PASSED_TO_MESS_HANDLER
                    *"----------------------------------------------------------------------

                    INCLUDE lrsalk01.

                    STATICS: s_save_requnr TYPE sbiwa_s_interface-requnr,
                            s_maximum_size LIKE sy-tabix,
                            s_flg_no_more_data,
                            s_flg_cursor,
                            s_estruc       LIKE tmcexact-estruc,
                            s_setupstruc   LIKE tmcexact-estruc,
                            s_t_select     LIKE rsselect OCCURS 0 WITH HEADER LINE.

                    DATA: gotstate LIKE dcobjif-gotstate,
                            e_source TYPE rsaot_s_osource,
                            id       LIKE mc04p_0matsetup-id,
                            counter  LIKE sy-tabix,
                            cond_select(1),
                            BEGIN OF cond_select_tab OCCURS 0,
                            sign   LIKE i_t_select-sign,
                            option LIKE i_t_select-option,
                            low    LIKE i_t_select-low,
                            high   LIKE i_t_select-high,
                            END OF cond_select_tab.

                    FIELD-SYMBOLS: <fs_field>.

                    * Definition für Anschluss BAdI
                    CLASS cl_exithandler DEFINITION LOAD.
                    DATA: exit  TYPE REF TO if_ex_mcex,
                            mcapp TYPE mcapp.
                    data: lv_badi_mmpur_scl TYPE REF TO badi_mmpur_bi_scl.
                    CALL METHOD cl_exithandler=>get_instance
                        CHANGING
                        instance = exit.

                    *...initialization.....................................................*
                    IF NOT i_initflag EQ false.
                    *   save select-options
                        s_t_select[] = i_t_select[].

                    *   save maximum block size
                        s_maximum_size = i_maxsize.

                    *   save request number
                        s_save_requnr = i_requnr.

                    * save extractstructure

                        CALL FUNCTION 'RSA1_SINGLE_OLTPSOURCE_GET'
                        EXPORTING
                            i_oltpsource   = i_isource
                        IMPORTING
                            e_s_oltpsource = e_source
                        EXCEPTIONS
                            no_authority   = 1
                            not_exist      = 2
                            inconsistent   = 3
                            OTHERS         = 4.
                        IF sy-subrc NE 0.
                        log_write 'E'                    "message type
                                    'MCEX'                 "message class
                                    '007'                  "message number
                                    i_isource              "message variable 1
                                    s_estruc.              "message variable 2
                        RAISE error_passed_to_mess_handler.
                        ENDIF.
                        s_estruc = e_source-exstruct.

                    * Only updatemode C and F, allow I for IM, allow S for early delta
                        IF i_updmode NE 'C' AND i_updmode NE 'F'
                        AND ( i_updmode NE 'I' OR i_isource NE '2LIS_03_BX' )
                        AND i_updmode NE 'S'.
                        log_write 'E'                    "message type
                                    'MCEX'                 "message class
                                    '008'                  "message number
                                    i_isource              "message variable 1
                                    s_estruc.              "message variable 2
                        RAISE error_passed_to_mess_handler.
                        ENDIF.

                    * check if setup-table exists

                        CASE s_estruc.
                        WHEN 'MC05Q1_0INSP'.
                            s_setupstruc = 'MC05Q1_INSPSETUP'.
                        WHEN 'MC05Q2_0INSP'.
                            s_setupstruc = 'MC05Q2_INSPSETUP'.
                        WHEN OTHERS.
                            CONCATENATE s_estruc 'SETUP' INTO s_setupstruc.
                        ENDCASE.

                        CALL FUNCTION 'DDIF_TABL_GET'
                        EXPORTING
                            name          = s_setupstruc
                        IMPORTING
                            gotstate      = gotstate
                        EXCEPTIONS
                            illegal_input = 1
                            OTHERS        = 2.
                        IF sy-subrc NE 0 OR
                        gotstate IS INITIAL.
                        log_write 'E'                    "message type
                                    'MCEX'                 "message class
                                    '009'                  "message number
                                    s_setupstruc           "message variable 1
                                    i_isource.             "message variable 2
                        RAISE error_passed_to_mess_handler.
                        ENDIF.

                        s_flg_no_more_data = false.
                    *   end of initialization
                    ELSE.
                    *   check request number
                        IF i_requnr NE s_save_requnr.
                        log_write 'E'                    "message type
                                    'M+'                   "message class
                                    '038'                  "message number
                                    i_requnr               "message variable 1
                                    s_save_requnr.         "message variable 2
                        RAISE error_passed_to_mess_handler.
                        ENDIF.

                    *   Accept init simulation (mode 'S') for early delta
                        IF i_updmode EQ 'S'.
                        RAISE no_more_data.
                        ENDIF.

                    *   clear tables for data
                        CLEAR:   e_t_data.
                        REFRESH: e_t_data.

                    *   check, if all data has been read
                        IF s_flg_no_more_data EQ true.
                        RAISE no_more_data.
                        ENDIF.

                    * open cursor for selection only one time
                        IF s_flg_cursor EQ false.
                        CASE s_estruc.           "begin 1727757
                            WHEN 'MC02M_0HDR'
                            OR 'MC02M_0ITM'
                            OR 'MC02M_0SCL'
                            OR 'MC02M_0SCN'
                            OR 'MC02M_0ACC'
                            OR 'MC02M_0CGR'
                            OR 'MC02M_0SGR'.
                            OPEN CURSOR WITH HOLD g_cursor FOR
                                SELECT id FROM (s_setupstruc)
                                WHERE srtf2 = 0 ORDER BY id.
                            WHEN OTHERS.
                            OPEN CURSOR WITH HOLD g_cursor FOR
                                SELECT id FROM (s_setupstruc)
                            WHERE srtf2 = 0.
                        ENDCASE.                  "end 1727757
                        s_flg_cursor = true.
                        SUBTRACT 1 FROM s_maximum_size.
                        ENDIF.

                    * macro for every infosource / extract structure
                    ***** start macro *****
                        DEFINE sel.
                        when &1.
                        sort s_t_select by fieldnm.                  "PI 2000.2 SP_353722
                        do.
                            fetch next cursor g_cursor into id.
                            if sy-subrc eq 0.
                            import &2 from database &3(bw) id id.
                            loop at &2.
                                cond_select = true.
                                loop at s_t_select.
                    * "delete" entry if select-option does not fit
                                assign component s_t_select-fieldnm of
                                    structure &2 to <fs_field>.
                                if sy-subrc ne 0.
                                    log_write 'E'          "message type
                                            'MCEX'       "message class
                                            '022'        "message number
                                            i_isource    "message variable 1
                                            s_estruc.    "message variable 2
                                    raise error_passed_to_mess_handler.
                                endif.
                    *             refresh cond_select_tab.             "PI 2000.2 SP_353722
                                move-corresponding s_t_select to cond_select_tab.
                                append cond_select_tab.
                                at end of fieldnm.                   "PI 2000.2 SP_353722
                                    if not <fs_field> in cond_select_tab.
                                    cond_select = false.
                                    refresh cond_select_tab.         "PI 2000.2 SP_353722
                                    exit.
                                    endif.
                                    refresh cond_select_tab.             "PI 2000.2 SP_353722
                                endat.                               "PI 2000.2 SP_353722
                                endloop.
                                if cond_select eq true.
                                append &2 to e_t_data.
                                add 1 to counter.
                                endif.
                            endloop.
                            if s_maximum_size le counter.
                                exit.
                            endif.
                            else.
                            close cursor g_cursor.
                            s_flg_no_more_data = true.
                            exit.
                            endif.
                        enddo.
                        END-OF-DEFINITION.
                    ***** end macro *****
                    ENHANCEMENT-POINT EHP603_LMCEXU06_01 SPOTS ES_SAPLMCEX.
                    *$*$-Start: EHP603_LMCEXU06_01------------------------------------------------------------------$*$*
                    ENHANCEMENT 7  MM_BUYER_SFWS_SC2_LMCEXU03.    "active version
                    CASE s_estruc.
                    * MM PUR Service
                    sel 'MC02M_0SRV'   mc02m_0srv_tab    mc02m_0srvsetup.
                    ENDCASE.
                    ENDENHANCEMENT.
                    *$*$-End:   EHP603_LMCEXU06_01------------------------------------------------------------------$*$*


                    ENHANCEMENT-POINT EHP_MCEX_BW_LO_API_002 SPOTS ES_SAPLMCEX.
                    *$*$-Start: EHP_MCEX_BW_LO_API_002--------------------------------------------------------------$*$*
                    ENHANCEMENT 1  MCEX_SFWS_MAN_LMCEXU06.    "active version
                    CASE s_estruc.
                    *-- Application '04'
                    sel 'MC04PK0KAN'   mc04pk0kan_tab    mc04pk0kansetup.
                    ENDCASE.
                    ENDENHANCEMENT.
                    *$*$-End:   EHP_MCEX_BW_LO_API_002--------------------------------------------------------------$*$*

                    ENHANCEMENT-SECTION     EHP_MCEX_BW_LO_API_001 SPOTS ES_SAPLMCEX.
                        CASE s_estruc.
                    *           extractstruc   internal table    setup table
                    *-- MM-PUR
                            sel 'MC02M_0HDR'   mc02m_0hdr_tab    mc02m_0hdrsetup.
                            sel 'MC02M_0ITM'   mc02m_0itm_tab    mc02m_0itmsetup.
                            sel 'MC02M_0SCL'   mc02m_0scl_tab    mc02m_0sclsetup.
                            sel 'MC02M_0SGR'   mc02m_0sgr_tab    mc02m_0sgrsetup.
                            sel 'MC02M_0SCN'   mc02m_0scn_tab    mc02m_0scnsetup.
                            sel 'MC02M_0CGR'   mc02m_0cgr_tab    mc02m_0cgrsetup.
                            sel 'MC02M_0ACC'   mc02m_0acc_tab    mc02m_0accsetup.
                    *-- Application '03' (Inventory Management)
                            sel 'MC03BF0'      mc03bf0_tab       mc03bf0setup.
                            sel 'MC03BX0'      mc03bx0_tab       mc03bx0setup.
                            sel 'MC03UM0'      mc03um0_tab       mc03um0setup.
                    *-- Application '04'
                            sel 'MC04P_0MAT'   mc04p_0mat_tab    mc04p_0matsetup.
                            sel 'MC04P_0ARB'   mc04p_0arb_tab    mc04p_0arbsetup.
                            sel 'MC04P_0COM'   mc04p_0com_tab    mc04p_0comsetup.
                            sel 'MC04PE0MAT'   mc04pe0mat_tab    mc04pe0matsetup.
                            sel 'MC04PE0ARB'   mc04pe0arb_tab    mc04pe0arbsetup.
                            sel 'MC04PE0COM'   mc04pe0com_tab    mc04pe0comsetup.
                    *-- Application '05' (Quality Management)
                            sel 'MC05Q1_0INSP' mc05q1_0insp_tab  mc05q1_inspsetup.
                            sel 'MC05Q2_0INSP' mc05q2_0insp_tab  mc05q2_inspsetup.
                            sel 'MC05QV0UDN'   mc05qv0udn_tab    mc05qv0udnsetup.
                            sel 'MC05Q00NTF'   mc05q00ntf_tab    mc05q00ntfsetup.
                            sel 'MC05Q00ITM'   mc05q00itm_tab    mc05q00itmsetup.
                            sel 'MC05Q00CSE'   mc05q00cse_tab    mc05q00csesetup.
                            sel 'MC05Q00TSK'   mc05q00tsk_tab    mc05q00tsksetup.
                            sel 'MC05Q00ACT'   mc05q00act_tab    mc05q00actsetup.
                    *-- Application '06' (Invoice verification)
                            sel 'MC06M_0ITM'    mc06m_0itm_tab    mc06m_0itmsetup.
                    *-- Application '08 (Transportation)
                            sel 'MC08TR0FKP' mc08tr0fkp_tab  mc08tr0fkpsetup.
                            sel 'MC08TR0FKZ' mc08tr0fkz_tab  mc08tr0fkzsetup.
                            sel 'MC08TR0TK' mc08tr0tk_tab  mc08tr0tksetup.
                            sel 'MC08TR0TLP' mc08tr0tlp_tab  mc08tr0tlpsetup.
                            sel 'MC08TR0TS' mc08tr0ts_tab  mc08tr0tssetup.
                    *-- Application '11' (Sales)
                            sel 'MC11VA0HDR' mc11va0hdr_tab  mc11va0hdrsetup.
                            sel 'MC11VA0STH' mc11va0sth_tab  mc11va0sthsetup.
                            sel 'MC11VA0ITM' mc11va0itm_tab  mc11va0itmsetup.
                            sel 'MC11VA0STI' mc11va0sti_tab  mc11va0stisetup.
                            sel 'MC11VA0SCL' mc11va0scl_tab  mc11va0sclsetup.
                            sel 'MC11V_0ITM' mc11v_0itm_tab  mc11v_0itmsetup.
                            sel 'MC11V_0SCL' mc11v_0scl_tab  mc11v_0sclsetup.
                            sel 'MC11V_0SSL' mc11v_0ssl_tab  mc11v_0sslsetup.
                    *       SEL 'MC11V_0SSL' MC11V_0SSL_TAB  MC11V_0SCLSETUP.
                            sel 'MC11VA0KON' mc11va0kon_tab  mc11va0konsetup.
                    *-- Application '12' (Shipping)
                            sel 'MC12VC0HDR' mc12vc0hdr_tab  mc12vc0hdrsetup.
                            sel 'MC12VC0ITM' mc12vc0itm_tab  mc12vc0itmsetup.
                            sel 'MC12VC0SCL' mc12vc0scl_tab  mc12vc0sclsetup.
                    *-- Application '13' (Billing)
                            sel 'MC13VD0HDR' mc13vd0hdr_tab  mc13vd0hdrsetup.
                            sel 'MC13VD0ITM' mc13vd0itm_tab  mc13vd0itmsetup.
                            sel 'MC13VD0KON' mc13vd0kon_tab  mc13vd0konsetup.
                    *-- Application '17' (plant maintainance)
                            sel 'MC17I00NTF' mc17i00ntf_tab  mc17i00ntfsetup.
                            sel 'MC17I00ITM' mc17i00itm_tab  mc17i00itmsetup.
                            sel 'MC17I00CSE' mc17i00cse_tab  mc17i00csesetup.
                            sel 'MC17I00TSK' mc17i00tsk_tab  mc17i00tsksetup.
                            sel 'MC17I00ACT' mc17i00act_tab  mc17i00actsetup.
                            sel 'MC17I30HDR' mc17i30hdr_tab  mc17i30hdrsetup.
                            sel 'MC17I30OPR' mc17i30opr_tab  mc17i30oprsetup.

                    *-- Application '18' (customer service)
                            sel 'MC18I00NTF' mc18i00ntf_tab  mc18i00ntfsetup.
                            sel 'MC18I00ITM' mc18i00itm_tab  mc18i00itmsetup.
                            sel 'MC18I00CSE' mc18i00cse_tab  mc18i00csesetup.
                            sel 'MC18I00TSK' mc18i00tsk_tab  mc18i00tsksetup.
                            sel 'MC18I00ACT' mc18i00act_tab  mc18i00actsetup.
                            sel 'MC18I30HDR' mc18i30hdr_tab  mc18i30hdrsetup.
                            sel 'MC18I30OPR' mc18i30opr_tab  mc18i30oprsetup.
                    *-- Application (40)
                            sel 'MC40RP0REV' mc40rp0rev_tab  mc40rp0revsetup.
                    *-- Retail POS-Data Cashier
                            sel 'MC43RK0CAS' mc43rk0cas_tab  mc43rk0cassetup.
                    *-- Retail POS-Date Receipts
                            sel 'MC44RB0REC' mc44rb0rec_tab  mc44rb0recsetup.
                    *-- Agency Business
                            sel 'MC45W_0HDR' mc45w_0hdr_tab  mc45w_0hdrsetup.
                            sel 'MC45W_0ITM' mc45w_0itm_tab  mc45w_0itmsetup.
                            sel 'MC45W_0LST' mc45w_0lst_tab  mc45w_0lstsetup.
                            sel 'MC45PD0LST' mc45pd0lst_tab  mc45pd0lstsetup.
                            sel 'MC45CD0LST' mc45cd0lst_tab  mc45cd0lstsetup.
                            sel 'MC45VD0LST' mc45vd0lst_tab  mc45vd0lstsetup.
                            sel 'MC45VS0LST' mc45vs0lst_tab  mc45vs0lstsetup.
                        WHEN  'MC45W_0CWB'.
                            mcapp = s_estruc+2(2).
                            CALL METHOD exit->fill_data
                            EXPORTING
                                i_estruc                     = s_estruc
                                i_t_select                   = s_t_select[]
                                flt_val                      = mcapp
                                i_maximum_size               = s_maximum_size
                                i_source                     = i_isource
                            IMPORTING
                                e_t_data                     = e_t_data[]
                                e_flg_no_more_data           = s_flg_no_more_data
                            CHANGING
                                c_cursor                     = g_cursor
                            EXCEPTIONS
                                error_passed_to_mess_handler = 1.
                    ***** please insert your application specific data *****
                        WHEN OTHERS.
                            mcapp = s_estruc+2(2).
                            CALL METHOD exit->fill_data
                            EXPORTING
                                i_estruc                     = s_estruc
                                i_t_select                   = s_t_select[]
                                flt_val                      = mcapp
                                i_maximum_size               = s_maximum_size
                                i_source                     = i_isource
                            IMPORTING
                                e_t_data                     = e_t_data[]
                                e_flg_no_more_data           = s_flg_no_more_data
                            CHANGING
                                c_cursor                     = g_cursor
                            EXCEPTIONS
                                error_passed_to_mess_handler = 1.
                        ENDCASE.                           " s_estruc
                    END-ENHANCEMENT-SECTION.
                    *$*$-Start: EHP_MCEX_BW_LO_API_001--------------------------------------------------------------$*$*
                    ENHANCEMENT 1  MCEX_SFWS_SC_LMCEXU06.    "active version
                        CASE s_estruc.
                    *           extractstruc   internal table    setup table
                    *-- MM-PUR
                            sel 'MC02M_0HDR'   mc02m_0hdr_tab    mc02m_0hdrsetup.
                            sel 'MC02M_0ITM'   mc02m_0itm_tab    mc02m_0itmsetup.
                            sel 'MC02M_0SCL'   mc02m_0scl_tab    mc02m_0sclsetup.
                            sel 'MC02M_0SGR'   mc02m_0sgr_tab    mc02m_0sgrsetup.
                            sel 'MC02M_0SCN'   mc02m_0scn_tab    mc02m_0scnsetup.
                            sel 'MC02M_0CGR'   mc02m_0cgr_tab    mc02m_0cgrsetup.
                            sel 'MC02M_0ACC'   mc02m_0acc_tab    mc02m_0accsetup.
                    *-- Application '03' (Inventory Management)
                            sel 'MC03BF0'      mc03bf0_tab       mc03bf0setup.
                            sel 'MC03BX0'      mc03bx0_tab       mc03bx0setup.
                            sel 'MC03UM0'      mc03um0_tab       mc03um0setup.
                    *-- Application '04'
                            sel 'MC04P_0MAT'   mc04p_0mat_tab    mc04p_0matsetup.
                            sel 'MC04P_0ARB'   mc04p_0arb_tab    mc04p_0arbsetup.
                            sel 'MC04P_0COM'   mc04p_0com_tab    mc04p_0comsetup.
                            sel 'MC04PE0MAT'   mc04pe0mat_tab    mc04pe0matsetup.
                            sel 'MC04PE0ARB'   mc04pe0arb_tab    mc04pe0arbsetup.
                            sel 'MC04PE0COM'   mc04pe0com_tab    mc04pe0comsetup.
                    *-- Application '05' (Quality Management)
                            sel 'MC05Q1_0INSP' mc05q1_0insp_tab  mc05q1_inspsetup.
                            sel 'MC05Q2_0INSP' mc05q2_0insp_tab  mc05q2_inspsetup.
                            sel 'MC05QV0UDN'   mc05qv0udn_tab    mc05qv0udnsetup.
                            sel 'MC05Q00NTF'   mc05q00ntf_tab    mc05q00ntfsetup.
                            sel 'MC05Q00ITM'   mc05q00itm_tab    mc05q00itmsetup.
                            sel 'MC05Q00CSE'   mc05q00cse_tab    mc05q00csesetup.
                            sel 'MC05Q00TSK'   mc05q00tsk_tab    mc05q00tsksetup.
                            sel 'MC05Q00ACT'   mc05q00act_tab    mc05q00actsetup.
                    *-- Application '06' (Invoice verification)
                            sel 'MC06M_0ITM'    mc06m_0itm_tab    mc06m_0itmsetup.
                    *-- Application '08 (Transportation)
                            sel 'MC08TR0FKP' mc08tr0fkp_tab  mc08tr0fkpsetup.
                            sel 'MC08TR0FKZ' mc08tr0fkz_tab  mc08tr0fkzsetup.
                            sel 'MC08TR0TK' mc08tr0tk_tab  mc08tr0tksetup.
                            sel 'MC08TR0TLP' mc08tr0tlp_tab  mc08tr0tlpsetup.
                            sel 'MC08TR0TS' mc08tr0ts_tab  mc08tr0tssetup.
                    *-- Application '11' (Sales)
                            sel 'MC11VA0HDR' mc11va0hdr_tab  mc11va0hdrsetup.
                            sel 'MC11VA0STH' mc11va0sth_tab  mc11va0sthsetup.
                            sel 'MC11VA0ITM' mc11va0itm_tab  mc11va0itmsetup.
                            sel 'MC11VA0STI' mc11va0sti_tab  mc11va0stisetup.
                            sel 'MC11VA0SCL' mc11va0scl_tab  mc11va0sclsetup.
                            sel 'MC11V_0ITM' mc11v_0itm_tab  mc11v_0itmsetup.
                            sel 'MC11V_0SCL' mc11v_0scl_tab  mc11v_0sclsetup.
                            sel 'MC11V_0SSL' mc11v_0ssl_tab  mc11v_0sslsetup.
                    *       SEL 'MC11V_0SSL' MC11V_0SSL_TAB  MC11V_0SCLSETUP.
                            sel 'MC11VA0KON' mc11va0kon_tab  mc11va0konsetup.
                    *-- Application '12' (Shipping)
                            sel 'MC12VC0HDR' mc12vc0hdr_tab  mc12vc0hdrsetup.
                            sel 'MC12VC0ITM' mc12vc0itm_tab  mc12vc0itmsetup.
                            sel 'MC12VC0SCL' mc12vc0scl_tab  mc12vc0sclsetup.
                    *-- Application '13' (Billing)
                            sel 'MC13VD0HDR' mc13vd0hdr_tab  mc13vd0hdrsetup.
                            sel 'MC13VD0ITM' mc13vd0itm_tab  mc13vd0itmsetup.
                            sel 'MC13VD0KON' mc13vd0kon_tab  mc13vd0konsetup.
                    *-- Application '17' (plant maintainance)
                            sel 'MC17I00NTF' mc17i00ntf_tab  mc17i00ntfsetup.
                            sel 'MC17I00ITM' mc17i00itm_tab  mc17i00itmsetup.
                            sel 'MC17I00CSE' mc17i00cse_tab  mc17i00csesetup.
                            sel 'MC17I00TSK' mc17i00tsk_tab  mc17i00tsksetup.
                            sel 'MC17I00ACT' mc17i00act_tab  mc17i00actsetup.
                            sel 'MC17I30HDR' mc17i30hdr_tab  mc17i30hdrsetup.
                            sel 'MC17I30OPR' mc17i30opr_tab  mc17i30oprsetup.

                    *-- Application '18' (customer service)
                            sel 'MC18I00NTF' mc18i00ntf_tab  mc18i00ntfsetup.
                            sel 'MC18I00ITM' mc18i00itm_tab  mc18i00itmsetup.
                            sel 'MC18I00CSE' mc18i00cse_tab  mc18i00csesetup.
                            sel 'MC18I00TSK' mc18i00tsk_tab  mc18i00tsksetup.
                            sel 'MC18I00ACT' mc18i00act_tab  mc18i00actsetup.
                            sel 'MC18I30HDR' mc18i30hdr_tab  mc18i30hdrsetup.
                            sel 'MC18I30OPR' mc18i30opr_tab  mc18i30oprsetup.
                    *-- Application (40)
                            sel 'MC40RP0REV' mc40rp0rev_tab  mc40rp0revsetup.
                    *-- Retail POS-Data Cashier
                            sel 'MC43RK0CAS' mc43rk0cas_tab  mc43rk0cassetup.
                    *-- Retail POS-Date Receipts
                            sel 'MC44RB0REC' mc44rb0rec_tab  mc44rb0recsetup.
                    *-- Agency Business
                            sel 'MC45W_0HDR' mc45w_0hdr_tab  mc45w_0hdrsetup.
                            sel 'MC45W_0ITM' mc45w_0itm_tab  mc45w_0itmsetup.
                            sel 'MC45W_0LST' mc45w_0lst_tab  mc45w_0lstsetup.
                            sel 'MC45PD0LST' mc45pd0lst_tab  mc45pd0lstsetup.
                            sel 'MC45CD0LST' mc45cd0lst_tab  mc45cd0lstsetup.
                            sel 'MC45VD0LST' mc45vd0lst_tab  mc45vd0lstsetup.
                            sel 'MC45VS0LST' mc45vs0lst_tab  mc45vs0lstsetup.
                        WHEN  'MC45W_0CWB'.
                            mcapp = s_estruc+2(2).
                            CALL METHOD exit->fill_data
                            EXPORTING
                                i_estruc                     = s_estruc
                                i_t_select                   = s_t_select[]
                                flt_val                      = mcapp
                                i_maximum_size               = s_maximum_size
                                i_source                     = i_isource
                            IMPORTING
                                e_t_data                     = e_t_data[]
                                e_flg_no_more_data           = s_flg_no_more_data
                            CHANGING
                                c_cursor                     = g_cursor
                            EXCEPTIONS
                                error_passed_to_mess_handler = 1.
                    *-- Application '47' (Rebate Management)
                            sel 'MC47M_0BV'  mc47m_0bv_tab   mc47m_0bvsetup.
                    ***** please insert your application specific data *****
                        WHEN OTHERS.
                            mcapp = s_estruc+2(2).
                            CALL METHOD exit->fill_data
                            EXPORTING
                                i_estruc                     = s_estruc
                                i_t_select                   = s_t_select[]
                                flt_val                      = mcapp
                                i_maximum_size               = s_maximum_size
                                i_source                     = i_isource
                            IMPORTING
                                e_t_data                     = e_t_data[]
                                e_flg_no_more_data           = s_flg_no_more_data
                            CHANGING
                                c_cursor                     = g_cursor
                            EXCEPTIONS
                                error_passed_to_mess_handler = 1.
                        ENDCASE.                           " s_estruc
                    ENDENHANCEMENT.
                    *$*$-End:   EHP_MCEX_BW_LO_API_001--------------------------------------------------------------$*$*

                    ENHANCEMENT-POINT EHP_MCEX_BW_LO_API_01 SPOTS ES_SAPLMCEX.
                    *$*$-Start: EHP_MCEX_BW_LO_API_01---------------------------------------------------------------$*$*
                    ENHANCEMENT 1  MCEX_SFWS_SC1_LMCEXU06.    "active version
                    *EHP Implementation for DEIG (STO) BADI Calls
                        TRY.
                            GET BADI lv_badi_mmpur_scl.
                        CATCH cx_badi_not_implemented.                  "#EC NO_HANDLER
                        ENDTRY.
                    try.
                        CALL BADI LV_BADI_MMPUR_SCL->CHANGE_STO
                        EXPORTING
                            I_ESTRUC           = s_estruc
                            I_T_SELECT         = s_t_select[]
                            FLT_VAL            = mcapp
                            I_MAXIMUM_SIZE     = s_maximum_size
                            I_SOURCE           = i_isource
                        CHANGING
                            C_T_DATA           = e_t_data[]
                            C_FLG_NO_MORE_DATA = s_flg_no_more_data
                            C_CURSOR           = g_cursor
                            .
                    CATCH
                            cx_badi_initial_reference.
                    ENDTRY.
                    ENDENHANCEMENT.
                    *$*$-End:   EHP_MCEX_BW_LO_API_01---------------------------------------------------------------$*$*
                    ENDIF.                               " initialization

                    ENDFUNCTION.

                  # """
)

agent.print_response("List me SAP tables used to extract data for 2LIS_11_VAITM.")