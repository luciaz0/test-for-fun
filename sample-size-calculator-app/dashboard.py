import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from ab_calculator_logic import calculate_binary_sample_size, calculate_mean_sample_size

st.set_page_config(page_title="A/B Test Sample Size Calculator", layout="wide")

st.title("📊 A/B Test Sample Size Calculator")
st.markdown("""
This tool automates sample size calculations for A/B tests based on your experiment design.
Select the metric type and input your parameters below.
""")

tab1, tab2 = st.tabs(["Binary Metric (e.g. CVR)", "Mean Metric (e.g. GMV)"])

with tab1:
    st.header("Binary Metric Calculation")
    col1, col2 = st.columns(2)
    
    with col1:
        p1 = st.number_input("Baseline Proportion (p1)", min_value=0.01, max_value=0.99, value=0.10, step=0.01, format="%.2f", help="The current conversion rate or proportion.")
        mde = st.number_input("Minimum Detectable Effect (Relative %)", min_value=0.001, max_value=1.0, value=0.05, step=0.005, format="%.3f", help="The relative lift you want to detect (e.g., 0.05 for 5%).")
        num_test_groups = st.number_input("Number of Test Groups (excluding control)", min_value=1, max_value=10, value=1, key="binary_groups")
        
    with col2:
        alpha = st.number_input("Significance Level (Alpha)", min_value=0.01, max_value=0.20, value=0.05, step=0.01, key="binary_alpha")
        power = st.number_input("Statistical Power", min_value=0.50, max_value=0.99, value=0.80, step=0.05, key="binary_power")
        
    if st.button("Calculate Binary Sample Size"):
        result = calculate_binary_sample_size(p1, mde, num_test_groups, alpha, power)
        
        if result:
            st.success("Calculation Complete!")
            c1, c2, c3 = st.columns(3)
            c1.metric("Sample Size Per Group", f"{result['sample_size_per_group']:,}")
            c2.metric("Target Metric Value", f"{result['test_target_metric']:.4f}")
            c3.metric("Total Sample Size", f"{result['total_sample_size']:,}")
            
            # Sensitivity Analysis Plot
            st.subheader("Sensitivity Analysis: Sample Size vs MDE")
            mde_range = np.linspace(max(0.001, mde*0.5), mde*2, 20)
            sizes = [calculate_binary_sample_size(p1, m, num_test_groups, alpha, power)['sample_size_per_group'] for m in mde_range]
            
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=mde_range, y=sizes, mode='lines+markers', name='Sample Size'))
            fig.update_layout(xaxis_title="Relative MDE", yaxis_title="Sample Size Per Group", template="plotly_white")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.error("Invalid input parameters. p2 (p1 * (1+mde)) must be between 0 and 1.")

with tab2:
    st.header("Mean-based Metric Calculation")
    col1, col2 = st.columns(2)
    
    with col1:
        mean = st.number_input("Baseline Mean", min_value=0.0, value=100.0, step=1.0)
        std_dev = st.number_input("Standard Deviation", min_value=0.1, value=50.0, step=1.0)
        mde_abs = st.number_input("Absolute MDE (Change in Mean)", min_value=0.01, value=5.0, step=0.5)
        
    with col2:
        num_test_groups_m = st.number_input("Number of Test Groups (excluding control)", min_value=1, max_value=10, value=1, key="mean_groups")
        alpha_m = st.number_input("Significance Level (Alpha)", min_value=0.01, max_value=0.20, value=0.05, step=0.01, key="mean_alpha")
        power_m = st.number_input("Statistical Power", min_value=0.50, max_value=0.99, value=0.80, step=0.05, key="mean_power")
        
    if st.button("Calculate Mean Sample Size"):
        result = calculate_mean_sample_size(mean, std_dev, mde_abs, num_test_groups_m, alpha_m, power_m)
        
        st.success("Calculation Complete!")
        c1, c2, c3 = st.columns(3)
        c1.metric("Sample Size Per Group", f"{result['sample_size_per_group']:,}")
        c2.metric("Target Mean", f"{result['test_target_metric']:.2f}")
        c3.metric("Total Sample Size", f"{result['total_sample_size']:,}")

        # Sensitivity Analysis Plot
        st.subheader("Sensitivity Analysis: Sample Size vs MDE")
        mde_range = np.linspace(max(0.01, mde_abs*0.5), mde_abs*2, 20)
        sizes = [calculate_mean_sample_size(mean, std_dev, m, num_test_groups_m, alpha_m, power_m)['sample_size_per_group'] for m in mde_range]
        
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=mde_range, y=sizes, mode='lines+markers', name='Sample Size'))
        fig.update_layout(xaxis_title="Absolute MDE", yaxis_title="Sample Size Per Group", template="plotly_white")
        st.plotly_chart(fig, use_container_width=True)

st.sidebar.title("About")
st.sidebar.info("""
This calculator uses:
- **NormalIndPower** for binary metrics (z-test for proportions).
- **TTestIndPower** for mean-based metrics (t-test).
- **Bonferroni Correction** for multiple test groups: `alpha_adj = alpha / num_test_groups`.
""")
