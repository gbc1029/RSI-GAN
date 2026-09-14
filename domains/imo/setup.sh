# Download the imobench repo
cd domains/imo
git clone https://github.com/google-deepmind/superhuman.git
cd superhuman
git checkout c1ee02e03d4cdb2ab21cd01ac927d895f5287fc8
cd ../
mv superhuman/imobench/*.csv ./
rm -rf superhuman
cd ../../

# Curate subsets
python -m domains.imo.curate_subsets
